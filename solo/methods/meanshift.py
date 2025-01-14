# Copyright 2022 solo-learn development team.

# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to use,
# copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the
# Software, and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies
# or substantial portions of the Software.

# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED,
# INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR
# PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE
# FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
# OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import argparse
from typing import Any, Dict, List, Sequence, Tuple

import torch
import torch.nn as nn
import torch.nn.functional as F
from solo.losses.meanshift import mean_shift_loss_func
from solo.methods.base import BaseMomentumMethod
from solo.utils.misc import gather
from solo.utils.momentum import initialize_momentum_params


class MeanShift(BaseMomentumMethod):
    def __init__(
        self,
        proj_output_dim: int,
        proj_hidden_dim: int,
        pred_hidden_dim: int,
        num_neighbors: int,
        queue_size: int,
        **kwargs,
    ):

        super().__init__(**kwargs)

        # projector
        self.projector = nn.Sequential(
            nn.Linear(self.features_dim, proj_hidden_dim),
            nn.BatchNorm1d(proj_hidden_dim),
            nn.ReLU(),
            nn.Linear(proj_hidden_dim, proj_output_dim),
        )

        # momentum projector
        self.momentum_projector = nn.Sequential(
            nn.Linear(self.features_dim, proj_hidden_dim),
            nn.BatchNorm1d(proj_hidden_dim),
            nn.ReLU(),
            nn.Linear(proj_hidden_dim, proj_output_dim),
        )
        initialize_momentum_params(self.projector, self.momentum_projector)

        # predictor
        self.predictor = nn.Sequential(
            nn.Linear(proj_output_dim, pred_hidden_dim),
            nn.BatchNorm1d(pred_hidden_dim),
            nn.ReLU(),
            nn.Linear(pred_hidden_dim, proj_output_dim),
        )

        self.num_neighbors = num_neighbors
        self.queue_size = queue_size

        self.register_buffer("queue", torch.randn(self.queue_size, proj_output_dim))
        self.register_buffer("queue_ptr", torch.zeros(1, dtype=torch.long))
        self.queue = F.normalize(self.queue, dim=1)
        self.queue_once_traversed = False

    @staticmethod
    def add_model_specific_args(parent_parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
        parent_parser = super(MeanShift, MeanShift).add_model_specific_args(parent_parser)
        parser = parent_parser.add_argument_group("mean_shift")

        # projector
        parser.add_argument("--proj_output_dim", type=int, default=256)
        parser.add_argument("--proj_hidden_dim", type=int, default=2048)

        # predictor
        parser.add_argument("--pred_hidden_dim", type=int, default=512)
        parser.add_argument("--num_neighbors", type=int, default=5)
        parser.add_argument("--queue_size", type=int, default=65536)

        return parent_parser

    @property
    def learnable_params(self) -> List[dict]:
        """Adds projector and predictor parameters to the parent's learnable parameters.

        Returns:
            List[dict]: list of learnable parameters.
        """

        extra_learnable_params = [
            {"params": self.projector.parameters()},
            {"params": self.predictor.parameters()},
        ]
        return super().learnable_params + extra_learnable_params

    @property
    def momentum_pairs(self) -> List[Tuple[Any, Any]]:
        """Adds (projector, momentum_projector) to the parent's momentum pairs.

        Returns:
            List[Tuple[Any, Any]]: list of momentum pairs.
        """

        extra_momentum_pairs = [(self.projector, self.momentum_projector)]
        return super().momentum_pairs + extra_momentum_pairs

    @torch.no_grad()
    def dequeue_and_enqueue(self, k: torch.Tensor):
        """Adds new samples and removes old samples from the queue in a fifo manner.

        Args:
            z (torch.Tensor): batch of projected features.
        """

        k = gather(k)

        batch_size = k.shape[0]

        ptr = int(self.queue_ptr)  # type: ignore
        # assert self.queue_size % batch_size == 0

        self.queue[ptr : ptr + batch_size, :] = k

        if ptr + batch_size >= self.queue_size:
            self.queue_once_traversed = True

        ptr = (ptr + batch_size) % self.queue_size

        self.queue_ptr[0] = ptr  # type: ignore

    def forward(self, X: torch.Tensor, *args, **kwargs) -> Dict[str, Any]:
        """Performs forward pass of the online encoder (encoder, projector and predictor).

        Args:
            X (torch.Tensor): batch of images in tensor format.

        Returns:
            Dict[str, Any]: a dict containing the outputs of the parent and the logits of the head.
        """

        out = super().forward(X, *args, **kwargs)
        z = self.projector(out["feats"])
        p = self.predictor(z)
        return {**out, "z": z, "p": p}

    def training_step(self, batch: Sequence[Any], batch_idx: int) -> torch.Tensor:
        """Training step for MeanShift reusing BaseModel training step.

        Args:
            batch (Sequence[Any]): a batch of data in the format of [img_indexes, [X], Y], where
                [X] is a list of size self.num_crops containing batches of images.
            batch_idx (int): index of the batch.

        Returns:
            torch.Tensor: total loss composed of MeanShift and classification loss.
        """

        out = super().training_step(batch, batch_idx)
        class_loss = out["loss"]
        _, feats2 = out["feats"]
        momentum_feats1, _ = out["momentum_feats"]

        z2 = self.projector(feats2)
        p2 = self.predictor(z2)

        # forward momentum encoder
        with torch.no_grad():
            z1_momentum = self.momentum_projector(momentum_feats1)

        self.dequeue_and_enqueue(z1_momentum)

        # ------- meanshift loss -------
        mean_neg_cos_sim = (
            mean_shift_loss_func(
                p2, z1_momentum, self.queue[: self.queue_ptr[0]], self.num_neighbors
            )
            if not self.queue_once_traversed
            else mean_shift_loss_func(p2, z1_momentum, self.queue, self.num_neighbors)
        )
        mean_neg_cos_sim = mean_shift_loss_func(p2, z1_momentum, self.queue, self.num_neighbors)

        self.log("train_mean_neg_cos_sim", mean_neg_cos_sim, on_epoch=True, sync_dist=True)

        return mean_neg_cos_sim + class_loss
