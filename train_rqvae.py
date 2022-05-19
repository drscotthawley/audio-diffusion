#!/usr/bin/env python3
import argparse
from contextlib import contextmanager
from pathlib import Path
from random import randint
import sys
from glob import glob

# from einops import rearrange
import pytorch_lightning as pl
from pytorch_lightning.utilities.distributed import rank_zero_only
import torch
from torch.utils import data
import torchaudio
from torchaudio import transforms as T
import wandb

from dataset.dataset import SampleDataset
from diffusion.pqmf import CachedPQMF as PQMF
from diffusion.utils import PadCrop

from encoders.learner import SoundStreamXLLearner

# Define utility functions


@contextmanager
def train_mode(model, mode=True):
    """A context manager that places a model into training mode and restores
    the previous mode on exit."""
    modes = [module.training for module in model.modules()]
    try:
        yield model.train(mode)
    finally:
        for i, module in enumerate(model.modules()):
            module.training = modes[i]


def eval_mode(model):
    """A context manager that places a model into evaluation mode and restores
    the previous mode on exit."""
    return train_mode(model, False)


class DemoCallback(pl.Callback):
    def __init__(self, global_args):
        super().__init__()
        self.pqmf = PQMF(2, 70, global_args.pqmf_bands)
        self.demo_dir = global_args.demo_dir
        self.demo_samples = global_args.sample_size
        self.demo_every = global_args.demo_every
        self.demo_steps = global_args.demo_steps
        self.pad_crop = PadCrop(global_args.sample_size)

    @rank_zero_only
    @torch.no_grad()
    def on_train_batch_end(self, trainer, module, outputs, batch, batch_idx, unused=0):
        last_demo_step = -1
        if (trainer.global_step - 1) % self.demo_every != 0 or last_demo_step == trainer.global_step:
            return

        last_demo_step = trainer.global_step

        demo_files = glob(f'{self.demo_dir}/**/*.wav', recursive=True)

        audio_batch = torch.zeros(len(demo_files), 2, self.demo_samples)

        for i, demo_file in enumerate(demo_files):
            audio, sr = torchaudio.load(demo_file)
            audio = audio.clamp(-1, 1)
            audio = self.pad_crop(audio)
            audio_batch[i] = audio

        audio_batch = self.pqmf(audio_batch)

        audio_batch = audio_batch.to(module.device)

        with eval_mode(module):
            fakes = sample(module, audio_batch, self.demo_steps, 1)

        # undo the PQMF encoding
        fakes = self.pqmf.inverse(fakes.cpu())
        try:
            log_dict = {}
            for i, fake in enumerate(fakes):

                filename = f'demo_{trainer.global_step:08}_{i:02}.wav'
                fake = self.ms_encoder(fake).clamp(-1, 1).mul(32767).to(torch.int16).cpu()
                torchaudio.save(filename, fake, 44100)
                log_dict[f'demo_{i}'] = wandb.Audio(filename,
                                                    sample_rate=44100,
                                                    caption=f'Demo {i}')
            trainer.logger.experiment.log(log_dict, step=trainer.global_step)
        except Exception as e:
            print(f'{type(e).__name__}: {e}', file=sys.stderr)


class ExceptionCallback(pl.Callback):
    def on_exception(self, trainer, module, err):
        print(f'{type(err).__name__}: {err}', file=sys.stderr)

def main():
    p = argparse.ArgumentParser()
    p.add_argument('--training-dir', type=Path, required=True,
                   help='the training data directory')
    p.add_argument('--name', type=str, required=True,
                   help='the name of the run')
    p.add_argument('--demo-dir', type=Path, required=True,
                   help='path to a directory with audio files for demos')
    p.add_argument('--num-workers', type=int, default=4,
                   help='number of CPU workers for the DataLoader')
    p.add_argument('--batch-size', type=int, default=8,
                   help='number of audio samples per batch')
    p.add_argument('--num-gpus', type=int, default=1,
                   help='number of GPUs to use for training')
    p.add_argument('--sample-rate', type=int, default=48000,
                   help='The sample rate of the audio')
    p.add_argument('--sample-size', type=int, default=64000,
                   help='Number of samples to train on, must be a multiple of 640')
    p.add_argument('--demo-every', type=int, default=1000,
                   help='Number of steps between demos')                
    p.add_argument('--checkpoint-every', type=int, default=20000,
                   help='Number of steps between checkpoints')
    p.add_argument('--style-latent-size', type=int, default=512,
                   help='Size of the style latents')
    p.add_argument('--accum-batches', type=int, default=8,
                   help='Batches for gradient accumulation')                                 
    args = p.parse_args()

    train_set = SampleDataset([args.training_dir], args)
    sampler = data.RandomSampler(train_set, replacement=True, num_samples = len(train_set) * 5)
    train_dl = data.DataLoader(train_set, sampler=sampler, batch_size=args.batch_size,
                               num_workers=args.num_workers, persistent_workers=True, pin_memory=True)
    wandb_logger = pl.loggers.WandbLogger(project=args.name)

    last_checkpoint = pl.callbacks.ModelCheckpoint(every_n_train_steps=2000, filename="last")
    
    exc_callback = ExceptionCallback()

    soundstream = SoundStreamXLLearner(args)

    wandb_logger.watch(soundstream)

    latent_trainer = pl.Trainer(
        gpus=args.num_gpus,
        strategy="ddp_find_unused_parameters_false",
        #precision=16,
        accumulate_grad_batches=args.accum_batches,
        callbacks=[last_checkpoint, exc_callback],
        logger=wandb_logger,
        log_every_n_steps=1,
        max_epochs=100000,
    )

    latent_trainer.fit(soundstream, train_dl)


if __name__ == '__main__':
    main()
