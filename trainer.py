import argparse
import ast
from datetime import datetime

import pandas as pd
import torch
import torchbearer
from torch import nn, optim
from torchbearer import Trial
from torchbearer.callbacks import MultiStepLR, CosineAnnealingLR
from torchbearer.callbacks import TensorBoard, TensorBoardText, Cutout, CutMix, RandomErase

from datasets.datasets import ds, dstransforms, dsmeta
from implementations.torchbearer_implementation import FMix
from models.models import get_model
from utils import RMixup, MSDAAlternator, WarmupLR

device = "cuda" if torch.cuda.is_available() else "cpu"

# Setup
parser = argparse.ArgumentParser(description='PyTorch CIFAR10 Training')
parser.add_argument('--dataset', type=str, default='cifar10',
                    choices=['cifar10', 'cifar100', 'reduced_cifar', 'fashion', 'fashion_old', 'imagenet',
                             'tinyimagenet', 'flowers', 'caltech101', 'cars', 'fixedup', 'commands', 'birds'])
parser.add_argument('--dataset-path', type=str, default=None, help='Optional dataset path')
parser.add_argument('--split-fraction', type=float, default=1., help='Fraction of total data to train on for reduced_cifar dataset')
parser.add_argument('--model', default="ResNet18", type=str, help='model type')
parser.add_argument('--epoch', default=200, type=int, help='total epochs to run')
parser.add_argument('--train-steps', type=int, default=None, help='Number of training steps to run per "epoch"')
parser.add_argument('--lr', default=0.1, type=float, help='learning rate')
parser.add_argument('--lr-warmup', type=ast.literal_eval, default=False, help='Use lr warmup')
parser.add_argument('--batch-size', default=128, type=int, help='batch size')

parser.add_argument('--auto-augment', type=ast.literal_eval, default=False, help='Use auto augment with cifar10/100')
parser.add_argument('--augment', type=ast.literal_eval, default=True, help='use standard augmentation (default: True)')
parser.add_argument('--parallel', type=ast.literal_eval, default=False, help='Use DataParallel')
parser.add_argument('--reload', type=ast.literal_eval, default=False, help='Set to resume training from model path')
parser.add_argument('--verbose', type=int, default=2, choices=[0, 1, 2])
parser.add_argument('--seed', default=0, type=int, help='random seed')

# Augs
parser.add_argument('--random-erase', default=False, type=ast.literal_eval, help='Apply random erase')
parser.add_argument('--cutout', default=False, type=ast.literal_eval, help='Apply Cutout')
parser.add_argument('--msda-mode', default=None, type=str, choices=['fmix', 'cutmix', 'mixup', 'alt_mixup_fmix',
                                                                    'alt_mixup_cutmix', 'alt_fmix_cutmix'])

# Aug Params
parser.add_argument('--alpha', default=1., type=float, help='mixup/fmix interpolation coefficient')
parser.add_argument('--f-decay', default=3.0, type=float, help='decay power')
parser.add_argument('--t-alpha', default=1., type=float, help='target alpha')
parser.add_argument('--cutout_l', default=16, type=int, help='cutout/erase length')
parser.add_argument('--reformulate', default=False, type=ast.literal_eval, help='Use reformulated fmix/mixup')

# Scheduling
parser.add_argument('--schedule', type=int, nargs='+', default=[100, 150], help='Decrease learning rate at these epochs.')
parser.add_argument('--cosine-scheduler', type=ast.literal_eval, default=False, help='Set to use a cosine scheduler instead of step scheduler')

# Cross validation
parser.add_argument('--fold-path', type=str, default='./data/folds.npz', help='Path to object storing fold ids. Run-id 0 will regen this if not existing')
parser.add_argument('--n-folds', type=int, default=6, help='Number of cross val folds')
parser.add_argument('--fold', type=str, default='test', help='One of [1, ..., n] or "test"')

# Logs
parser.add_argument('--run-id', type=int, default=0, help='Run id')
parser.add_argument('--log-dir', default='./logs/testing', help='Tensorboard log dir')
parser.add_argument('--model-file', default='./saved_models/model.pt', help='Path under which to save model. eg ./model.py')
args = parser.parse_args()


if args.seed != 0:
    torch.manual_seed(args.seed)


print('==> Preparing data..')
data = ds[args.dataset]
meta = dsmeta[args.dataset]
classes, nc, size = meta['classes'], meta['nc'], meta['size']

transform_train, transform_test = dstransforms[args.dataset](args)
trainset, valset, testset = data(args)

trainloader = torch.utils.data.DataLoader(trainset, batch_size=args.batch_size, shuffle=True, num_workers=8)
valloader = torch.utils.data.DataLoader(valset, batch_size=args.batch_size, shuffle=True, num_workers=8)
testloader = torch.utils.data.DataLoader(testset, batch_size=args.batch_size, shuffle=True, num_workers=8)


print('==> Building model..')
net = get_model(args, classes, nc)
net = nn.DataParallel(net) if args.parallel else net
optimizer = optim.SGD(net.parameters(), lr=args.lr, momentum=0.9, weight_decay=1e-4)


print('==> Setting up callbacks..')
current_time = datetime.now().strftime('%b%d_%H-%M-%S') + "-run-" + str(args.run_id)
tboard = TensorBoard(write_graph=False, comment=current_time, log_dir=args.log_dir)
tboardtext = TensorBoardText(write_epoch_metrics=False, comment=current_time, log_dir=args.log_dir)


@torchbearer.callbacks.on_start
def write_params(_):
    params = vars(args)
    params['schedule'] = str(params['schedule'])
    df = pd.DataFrame(params, index=[0]).transpose()
    tboardtext.get_writer(tboardtext.log_dir).add_text('params', df.to_html(), 1)


modes = {
    'fmix': FMix(decay_power=args.f_decay, alpha=args.alpha, size=size, max_soft=0, reformulate=args.reformulate),
    'mixup': RMixup(args.alpha, reformulate=args.reformulate),
    'cutmix': CutMix(args.alpha, classes, True),
}
modes.update({
    'alt_mixup_fmix': MSDAAlternator(modes['fmix'], modes['mixup']),
    'alt_mixup_cutmix': MSDAAlternator(modes['mixup'], modes['cutmix']),
    'alt_fmix_cutmix': MSDAAlternator(modes['fmix'], modes['cutmix']),
})

cb = [tboard, tboardtext, write_params, torchbearer.callbacks.MostRecent(args.model_file)]
cb.append(modes[args.msda_mode]) if args.msda_mode is not None else []
cb.append(Cutout(1, args.cutout_l)) if args.cutout else []
cb.append(RandomErase(1, args.cutout_l)) if args.random_erase else []
# WARNING: Schedulers appear to be broken in some versions of PyTorch, including 1.4. We used 1.3.1
cb.append(MultiStepLR(args.schedule)) if not args.cosine_scheduler else cb.append(CosineAnnealingLR(args.epoch, eta_min=0.))
cb.append(WarmupLR(0.1, args.lr)) if args.lr_warmup else []


# FMix loss is equivalent to mixup loss and works for all msda in torchbearer
criterion = modes['fmix'].loss() if args.msda_mode is not None else nn.CrossEntropyLoss()

print('==> Training model..')
trial = Trial(net, optimizer, criterion, metrics=['acc', 'loss', 'lr'], callbacks=cb)
trial.with_generators(train_generator=trainloader, val_generator=valloader, train_steps=args.train_steps, test_generator=testloader).to(device)
if args.reload:
    state = torch.load(args.model_file)
    trial.load_state_dict(state)
    trial.replay()
trial.run(args.epoch, verbose=args.verbose)
trial.evaluate(data_key=torchbearer.TEST_DATA)
