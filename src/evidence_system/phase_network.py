"""CPU offline MS-TCN++ adapted to three-class post-removal phase segmentation.

Architecture follows Li et al., MS-TCN++ (TPAMI 2020),
https://arxiv.org/abs/2006.09220 and https://github.com/sj-li/MS-TCN2.
This reduced-width inference architecture preserves the frozen E71 parameter
names and intermediate padding masks. It does not train or select checkpoints.
The signal adaptation is not a reproduction of the original video benchmark.

Original implementation license (architecture/loss adapted here):
MIT License, Copyright (c) 2019 yabufarha
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:
The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.
THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from __future__ import annotations

import torch
from torch import nn
from torch.nn import functional as F

SETTINGS = {
    "Width": 32,
    "Dropout": 0.5,
    "RefinementStages": 3,
    "PredictionGenerationDilations": [[2 ** (9 - i), 2**i] for i in range(10)],
    "RefinementDilations": [2**i for i in range(10)],
}


class DualDilatedBlock(nn.Module):
    """The MS-TCN++ prediction-generation dual-dilation residual layer."""

    def __init__(self, width, first_dilation, second_dilation, dropout):
        super().__init__()
        self.first = nn.Conv1d(width, width, 3, padding=first_dilation, dilation=first_dilation)
        self.second = nn.Conv1d(width, width, 3, padding=second_dilation, dilation=second_dilation)
        self.fusion = nn.Conv1d(2 * width, width, 1)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x, mask):
        branches = torch.cat((self.first(x) * mask, self.second(x) * mask), dim=1)
        hidden = self.dropout(F.relu(self.fusion(branches)))
        return (x + hidden) * mask


class RefinementBlock(nn.Module):
    def __init__(self, width, dilation, dropout):
        super().__init__()
        self.temporal = nn.Conv1d(width, width, 3, padding=dilation, dilation=dilation)
        self.pointwise = nn.Conv1d(width, width, 1)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x, mask):
        hidden = F.relu(self.temporal(x)) * mask
        return (x + self.dropout(self.pointwise(hidden))) * mask


class PredictionGeneration(nn.Module):
    def __init__(self, input_width):
        super().__init__()
        width = SETTINGS["Width"]
        self.input_projection = nn.Conv1d(input_width, width, 1)
        self.blocks = nn.ModuleList(
            [
                DualDilatedBlock(width, first, second, SETTINGS["Dropout"])
                for first, second in SETTINGS["PredictionGenerationDilations"]
            ]
        )
        self.output_projection = nn.Conv1d(width, 3, 1)

    def forward(self, x, mask):
        hidden = self.input_projection(x) * mask
        for block in self.blocks:
            hidden = block(hidden, mask)
        return self.output_projection(hidden) * mask


class Refinement(nn.Module):
    """A refinement receives only the previous stage's class probabilities."""

    def __init__(self):
        super().__init__()
        width = SETTINGS["Width"]
        self.input_projection = nn.Conv1d(3, width, 1)
        self.blocks = nn.ModuleList(
            [
                RefinementBlock(width, dilation, SETTINGS["Dropout"])
                for dilation in SETTINGS["RefinementDilations"]
            ]
        )
        self.output_projection = nn.Conv1d(width, 3, 1)

    def forward(self, probabilities, mask):
        hidden = self.input_projection(probabilities) * mask
        for block in self.blocks:
            hidden = block(hidden, mask)
        return self.output_projection(hidden) * mask


class MSTCNPP(nn.Module):
    def __init__(self, input_width=96):
        super().__init__()
        self.prediction_generation = PredictionGeneration(input_width)
        self.refinements = nn.ModuleList(
            [Refinement() for _ in range(SETTINGS["RefinementStages"])]
        )

    def forward(self, x, valid_mask=None):
        """Return all stage logits [stage,batch,time,class]; no cross-record state."""
        if valid_mask is None:
            valid_mask = torch.ones(x.shape[:2], dtype=torch.bool, device=x.device)
        if valid_mask.shape != x.shape[:2] or valid_mask.dtype != torch.bool:
            raise ValueError("valid_mask must be a Boolean batch x time tensor")
        mask = valid_mask[:, None, :].to(x.dtype)
        logits = self.prediction_generation(x.transpose(1, 2) * mask, mask)
        stages = [logits.transpose(1, 2)]
        for refinement in self.refinements:
            logits = refinement(logits.softmax(dim=1) * mask, mask)
            stages.append(logits.transpose(1, 2))
        return torch.stack(stages)
