#!/bin/python3
# The MIT License (MIT)
# Copyright © 2021 Yuma Rao

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software,
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER
# DEALINGS IN THE SOFTWARE.

import argparse
import bittensor
import math
import torch
import transformers

from loguru import logger;

logger = logger.opt(colors=True)
from types import SimpleNamespace
import torch.nn as nn

import torch.nn.functional as F
from torch.nn import TransformerEncoder, TransformerEncoderLayer


class PositionalEncoding(nn.Module):

    def __init__(self, d_model: int, dropout: float, max_len: int = 5000):
        super().__init__()
        self.dropout = nn.Dropout(p=dropout)

        position = torch.arange(max_len).unsqueeze(1)
        div_term = torch.exp(torch.arange(0, d_model, 2) * (-math.log(10000.0) / d_model))
        pe = torch.zeros(max_len, 1, d_model)
        pe[:, 0, 0::2] = torch.sin(position * div_term)
        pe[:, 0, 1::2] = torch.cos(position * div_term)
        self.register_buffer('pe', pe)

    def forward(self, x: torch.tensor) -> torch.tensor:
        """
        Args:
            x: Tensor, shape [seq_len, batch_size, embedding_dim]
        """
        x = x + self.pe[:x.size(0)]
        return self.dropout(x)


class Nucleus(nn.Module):

    def __init__(self, config):
        super(Nucleus, self).__init__()
        self.config = config

        # Embedding Layer.
        self.embedding = nn.Embedding(bittensor.__vocab_size__, bittensor.__network_dim__)

        # Local Model
        local_layers = TransformerEncoderLayer(bittensor.__network_dim__, self.config.nucleus.nhead,
                                               self.config.nucleus.nhid, self.config.nucleus.dropout, activation='gelu')
        local_hidden_layers = TransformerEncoderLayer(bittensor.__network_dim__, self.config.nucleus.nhead,
                                                      self.config.nucleus.nhid, self.config.nucleus.dropout, activation='gelu')
        self.local_pos_encoder = PositionalEncoding(bittensor.__network_dim__, self.config.nucleus.dropout)
        self.local_encoder = TransformerEncoder(local_layers, self.config.nucleus.nlayers)
        self.local_hidden = TransformerEncoder(local_hidden_layers, self.config.nucleus.nlayers_local_hidden)
        self.local_decoder = nn.Linear(bittensor.__network_dim__, bittensor.__vocab_size__, bias=False)

        # Remote Model
        remote_context_layers = TransformerEncoderLayer(bittensor.__network_dim__, self.config.nucleus.nhead,
                                                        self.config.nucleus.nhid, self.config.nucleus.dropout, activation='gelu')
        self.remote_hidden = TransformerEncoder(remote_context_layers, self.config.nucleus.nlayers_remote_hidden)
        self.remote_decoder = nn.Linear(bittensor.__network_dim__, bittensor.__vocab_size__, bias=False)

        # From: https://github.com/huggingface/transformers/blob/master/examples/research_projects/distillation/distiller.py
        self.temperature = self.config.nucleus.temperature
        self.alpha_ce = self.config.nucleus.alpha_ce  # (cross-entropy) As measured by KL-divergence between distillation model next token predictions and teacher predictions.
        self.alpha_clm = self.config.nucleus.alpha_clm  # (contextual language modeling loss) Cross-entropy between local model logits (unnormalized scores) and target next token label one-hot encodings
        self.alpha_clm_dis = self.config.nucleus.alpha_clm_dis  # (clm distillation) Cross-entropy between distillation model logits and teacher soft labels.
        self.alpha_clm_rmt = self.config.nucleus.alpha_clm_rmt  # (clm remote) Cross-entropy between teacher features + task head and the target next token label one-hot encodings.
        self.alpha_mse = self.config.nucleus.alpha_mse  # Mean-square error between distillation model logits and teacher logits.
        self.alpha_mse_hid = self.config.nucleus.alpha_mse_hid  # Mean-square error between distillation model last hidden state and teacher last hidden state.
        self.alpha_cos = self.config.nucleus.alpha_cos  # (cosine distance) Cosine distance between distillation model last hidden state and teacher last hidden state.

        self.ce_loss_fct = nn.KLDivLoss(reduction="batchmean")
        self.lm_loss_fct = nn.CrossEntropyLoss(ignore_index=-100)
        # self.mse_loss_fct = nn.MSELoss(reduction="sum")  # results in disproportionately large loss, so opting for mean.
        self.mse_loss_fct = nn.MSELoss()
        self.mse_hid_loss_fct = nn.MSELoss()
        self.cosine_loss_fct = nn.CosineEmbeddingLoss(reduction="mean")

        self.peer_weights = nn.Parameter(torch.ones([0], requires_grad=True))
        self.noise_offset = 0.0000001
        self.init_weights()
        self.metagraph = None
        self.dendrite = None

    @staticmethod
    def add_args(parser: argparse.ArgumentParser):
        r""" Add custom params to the parser.
        """
        parser.add_argument('--nucleus.nhid', type=int,
                            help='the dimension of the feedforward network model in nn.TransformerEncoder', default=200)
        parser.add_argument('--nucleus.nhead', type=int, help='the number of heads in the multiheadattention models',
                            default=2)
        parser.add_argument('--nucleus.nlayers', type=int,
                            help='the number of nn.TransformerEncoderLayer in nn.TransformerEncoder', default=2)
        parser.add_argument('--nucleus.dropout', type=float, help='the dropout value', default=0.2)
        parser.add_argument('--nucleus.topk', type=int,
                            help='the number of peers queried during each remote forward call', default=20)
        parser.add_argument('--nucleus.punishment', type=float,
                            help='The punishment on the chain weights that do not respond ', default=0.001)

    def init_weights(self):
        initrange = 0.1
        self.remote_decoder.weight.data.uniform_(-initrange, initrange)
        self.local_decoder.weight.data.uniform_(-initrange, initrange)

    def compute_scores(self, loss):
        """Computes salience scores for each peer in the network w.r.t the loss.
        We use a simplified fishers information score. score_i = hessian_ii * peer_weight_i^2
        """
        peer_weights_d1 = \
        torch.autograd.grad(loss, self.peer_weights, create_graph=True, retain_graph=True, allow_unused=True)[0]
        if peer_weights_d1 == None: return torch.ones_like(self.peer_weights) * (
                    1 / self.metagraph().n.item())  # None if no grad w.r.t the chain weights.
        peer_weights_d2 = \
        torch.autograd.grad(peer_weights_d1.sum(), self.peer_weights, retain_graph=True, allow_unused=True)[0]
        validator_scores = peer_weights_d2 * (self.peer_weights ** 2) / 2
        return validator_scores

    def local_forward(self, inputs: torch.LongTensor, training: bool = True) -> SimpleNamespace:
        """ Forward pass through local transformer model of nucleus.
            Args:
                inputs (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_len)`, `required`):
                    Input batch of batch_size token sequences each of length sequence_len, where
                    each token is :obj:`torch.int64` in range [0, bittensor.__vocab_size__ - 1]
                training (:obj:`bool`), `optional`, defaults to True):
                    Switch to True if this forward pass computes a CLM loss.

            Returns:
                SimpleNamespace {
                    local_context (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `required`):
                        Transformer encoding produced using embedded inputs.
                    local_hidden (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__vocab_size__)`, `optional`):
                        Transformer encoding produced using local_context.
                    local_target (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__vocab_size__)`, `optional`):
                        Next token prediction logits produced using local_hidden.
                    local_target_loss (:obj:`torch.FloatTensor` of shape :obj:`(1)`, `optional`):
                        Next token prediction loss using local_hidden.
                    local_accuracy (:obj:`float`, `optional`):
                        Next token prediction accuracy using local_hidden.
                }
        """
        # To be filled.
        output = SimpleNamespace()

        # https://pytorch.org/docs/1.8.1/generated/torch.nn.Transformer.html#torch.nn.Transformer.forward
        # src: (S, N, E) the sequence to the encoder (required).
        # src_mask: (S, S) the mask for the src sequence (optional).
        # where S is the source sequence length, N is the batch size, E is the feature number

        # inputs.shape = [batch_size, sequence_len]
        sequence_len = inputs.shape[1]

        # src_mask: attention mask adds -inf to positions not allowed to attend, preventing forward-looking when
        #           predicting each token in the sequence.
        # src_mask.shape = [sequence_len, sequence_len]
        src_mask = torch.triu(torch.ones(sequence_len, sequence_len) * float('-inf'), diagonal=1)
        src_mask = src_mask.to(self.config.neuron.device)

        # embedding: retrieve learned representation vectors for input vocabulary tokens.
        # inputs.shape = [batch_size, sequence_len]
        # embedding.shape = [batch_size, sequence_len, bittensor.__network_dim__]
        embedding = self.embedding(inputs)

        # embedding.shape = [batch_size, sequence_len, bittensor.__network_dim__]
        # local_encoder expects embedding.shape = [sequence_len, batch_size, bittensor.__network_dim__]
        embedding = embedding.transpose(0, 1)

        # local_context: hidden layer encoding of sequence with local_context.
        # local_context.shape = [sequence_len, batch_size, bittensor.__network_dim__]
        local_context = self.local_encoder(embedding, mask=src_mask) * math.sqrt(bittensor.__network_dim__)

        # local_context: adding positional encoding to local_context.
        # local_context.shape = [sequence_len, batch_size, bittensor.__network_dim__]
        # local_context = self.local_pos_encoder(local_context)

        # external expects output.local_context.shape = [batch_size, sequence_len, bittensor.__network_dim__]
        output.local_context = local_context.transpose(0, 1)

        if training:
            # local_hidden: local model which learns a new projection from the local_context
            # local_hidden.shape = [sequence_len, batch_size, bittensor.__vocab_size__]
            # local_hidden = self.local_hidden(local_context.detach(), mask=src_mask)
            local_hidden = self.local_hidden(local_context, mask=src_mask)

            # external expects output.local_hidden.shape = [batch_size, sequence_len, bittensor.__network_dim__]
            output.local_hidden = local_hidden.transpose(0, 1)

            del local_hidden  # to help clear GPU memory to prevent OOM errors

            # local_target: projection of local_hidden onto target dimension.
            # local_target.shape = [batch_size, sequence_len, bittensor.__vocab_size__]
            output.local_target = self.local_decoder(output.local_hidden)

            # local_target_loss: MLM loss between local_target and passed targets.
            # local_target_loss.shape = [1]
            shift_logits = output.local_target[..., :-1, :].contiguous()
            shift_labels = inputs[..., 1:].contiguous()
            # if self.alpha_clm > 0.0:
            output.loss_clm = self.lm_loss_fct(shift_logits.view(-1, shift_logits.size(-1)),
                                               shift_labels.view(-1))

            predictions = shift_logits.detach().max(2).indices
            output.local_accuracy = (predictions == shift_labels).sum().item() / predictions.nelement()

            del shift_logits  # to help clear GPU memory to prevent OOM errors
            del shift_labels  # to help clear GPU memory to prevent OOM errors

            torch.cuda.empty_cache()  # to help clear GPU memory to prevent OOM errors

        return output

    def remote_forward(self, inputs: torch.LongTensor, training: bool = True, teacher_inputs: torch.LongTensor = None,
                       offset_mapping = None, offset_mapping2 = None,
                       batch_weights: torch.FloatTensor = None) -> SimpleNamespace:
        """ Forward pass inputs through the remote network and local transformer model, and produce distillation and
            next token prediction losses.
        Args:
            inputs (:obj:`torch.LongTensor` of shape :obj:`(batch_size, sequence_len)`, `required`):
                Input batch of batch_size token sequences each of length sequence_len, where
                each token is :obj:`torch.int64` in range [0, bittensor.__vocab_size__ - 1]
            training (:obj:`bool`), `optional`, defaults to True):
                Switch to True if this forward pass computes an MLM loss.
        Returns:
            self.local_forward() + SimpleNamespace {
                remote_context (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `required`):
                    Joined responses from the network.
                remote_hidden (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `required`):
                    Transformer encoding of remote_context.
                distillation_loss (:obj:`torch.FloatTensor` of shape :obj:`(1)`, `required`):
                    Distillation loss between local_context and remote_hidden.
                remote_target (:obj:`torch.FloatTensor` of shape :obj:`(batch_size,  bittensor.__vocab_size__)`, `optional`):
                    Target predictions using the remote_hidden layer.
                remote_target_loss (:obj:`torch.FloatTensor` of shape :obj:`(1)`, `optional`):
                    Next token prediction loss using remote_target.
            }
        """
        # Run local model
        output = self.local_forward(inputs, training)

        if teacher_inputs is not None:  # inclusion only for the distillation tests to bypass network remote and directly serve local teacher
            output.remote_context = teacher_inputs

        else:
            # remote_context: joined responses from a dendrite.forward_text call.
            # output.remote_context.shape = [batch_size, sequence_len (or block_size), bittensor.__network_dim__]
            output.remote_context = self.remote(inputs)

        # https://pytorch.org/docs/1.8.1/generated/torch.nn.Transformer.html#torch.nn.Transformer.forward
        # src: (S, N, E) the sequence to the encoder (required).
        # src_mask: (S, S) the mask for the src sequence (optional).
        # where S is the source sequence length, N is the batch size, E is the feature number

        # remote_context.shape = [sequence_len, batch_size, bittensor.__network_dim__]
        remote_context = output.remote_context.transpose(0, 1)

        # inputs.shape = [batch_size, sequence_len]
        batch_size = inputs.shape[0]
        sequence_len = inputs.shape[1]

        # offset_mapping: [batch_size, sequence_len] list of list of start index original token segmentation over input
        # offset_mapping2: [batch_size, sequence_len] list of list of start index expert token segmentation over input
        # compute mask for original segmentation to block future token info from expert embeddings
        # print(inputs.shape, offset_mapping.shape, offset_mapping2.shape)

        remote_hidden = []
        for i in range(batch_size):
            # src_mask: attention mask adds -inf to positions not allowed to attend, preventing forward-looking when
            #           predicting each next token in the sequence.
            # src_mask.shape = [sequence_len, sequence_len]
            src_mask = torch.zeros(sequence_len, sequence_len)
            # mask = [['*' for _ in range(sequence_len)] for _ in range(sequence_len)]

            seg = offset_mapping[i]
            seg2 = offset_mapping2[i]

            # print(seg, '\n', seg2)

            k = 0
            for j in range(sequence_len-1):
                pos = seg[j+1]  # edge position at input
                if seg2[k+1] > pos:  # after segment edge
                    src_mask[j, k:] = float('-inf')  # block future token info
                    # mask[j][k:] = [' '] * (sequence_len-1-k)
                    # print(j, k, pos, seg2[k+1], end=', ')
                else:
                    while k < sequence_len-1 and seg2[k+1] <= pos:  # before segment edge
                        k += 1
                    if k < sequence_len-1 and seg2[k+1] > pos:  # after segment edge
                        src_mask[j, k:] = float('-inf')  # block future token info
                        # mask[j][k:] = [' '] * (sequence_len-1-k)
                        # print(j, k, pos, seg2[k+1], end=', ')
            # print()
            # print('\n'.join([ll for ll in [''.join(l) for l in mask]]))
            src_mask[:, 0] = 0  # ensure at least one token is included to avoid NaNs
            src_mask = src_mask.to(self.config.neuron.device)

            # remote_hidden_seq: projects from the remote_context
            # remote_hidden_seq.shape = [sequence_len, bittensor.__network_dim__]
            remote_hidden += [self.remote_hidden(remote_context[:, i, :].unsqueeze(1), mask=src_mask)]

        remote_hidden = torch.cat(remote_hidden, dim=1)
        # print(remote_hidden, remote_hidden.shape)

        del remote_context  # to help clear GPU memory to prevent OOM errors

        # remote_hidden.shape = [sequence_len, batch_size, bittensor.__network_dim__]
        # external expects output.remote_hidden.shape = [batch_size, sequence_len, bittensor.__network_dim__]
        output.remote_hidden = remote_hidden.transpose(0, 1)

        del remote_hidden  # to help clear GPU memory to prevent OOM errors

        # distillation_loss : distillation loss between local_context and remote_context
        # distillation_loss.shape = [1]
        # This trains the local_context (student) to emulate the network context.
        # output.distillation_loss = F.mse_loss(output.local_context, output.remote_hidden.detach())
        if self.alpha_mse_hid > 0.0 or self.alpha_cos > 0.0:
            dim = output.local_context.shape[-1]
            s_hidden_states = output.local_context.reshape(-1, dim)  # (bs * seq_length, dim)
            t_hidden_states = output.remote_hidden.detach().reshape(-1, dim)  # (bs * seq_length, dim)

        if self.alpha_mse_hid > 0.0:
            if batch_weights is not None:
                output.loss_mse_hid = nn.MSELoss(reduction='none')(output.local_context, output.remote_hidden.detach())
                output.loss_mse_hid = (batch_weights * output.loss_mse_hid.mean(dim=(1, 2))).sum() / s_hidden_states.shape[0]
            else:
                output.loss_mse_hid = self.mse_hid_loss_fct(s_hidden_states, t_hidden_states) / s_hidden_states.shape[0]
            # output.loss_mse = self.mse_loss_fct(output.local_context, output.remote_hidden.detach()) / output.local_context.size(
            #     0
            # )  # Reproducing batchmean reduction
        else:
            output.loss_mse_hid = torch.tensor(0.)

        if self.alpha_cos > 0.0:
            target = s_hidden_states.new(s_hidden_states.size(0)).fill_(1)  # (bs * seq_length,)
            if batch_weights is not None:
                output.loss_cos = nn.CosineEmbeddingLoss(reduction='none')(output.local_context, output.remote_hidden.detach(), target)
                output.loss_cos = (batch_weights * output.loss_cos.mean(dim=(1, 2))).sum()
            else:
                output.loss_cos = self.cosine_loss_fct(s_hidden_states, t_hidden_states, target)

            del target
        else:
            output.loss_cos = torch.tensor(0.)

        if self.alpha_mse_hid > 0.0 or self.alpha_cos > 0.0:
            del s_hidden_states  # to help clear GPU memory to prevent OOM errors
            del t_hidden_states  # to help clear GPU memory to prevent OOM errors

        if training:
            # remote_target: projection of remote_hidden onto target dimension.
            # remote_target.shape = [batch_size, sequence_len, bittensor.__vocab_size__]
            output.remote_target = self.remote_decoder(output.remote_hidden)

            if self.alpha_clm_rmt > 0.0:
                # remote_target_loss: next token prediction loss between remote_target and passed targets.
                # remote_target_loss.shape = [1]
                shift_logits = output.remote_target[..., :-1, :].contiguous()

                shift_labels = inputs[..., 1:].contiguous()

                if batch_weights is not None:
                    output.loss_clm_rmt = nn.CrossEntropyLoss(reduction='none')(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))
                    output.loss_clm_rmt = (batch_weights.repeat_interleave(sequence_len-1) * output.loss_clm_rmt).sum() / (sequence_len-1.)
                else:
                    output.loss_clm_rmt = self.lm_loss_fct(shift_logits.view(-1, shift_logits.size(-1)), shift_labels.view(-1))

                predictions = shift_logits.detach().max(2).indices
                output.remote_accuracy = (predictions == shift_labels).sum().item() / predictions.nelement()
            else:
                output.loss_clm_rmt = torch.tensor(0.)
                output.remote_accuracy = torch.tensor(0.)

            if self.alpha_mse > 0.0 or self.alpha_ce > 0.0 or self.alpha_clm_dis > 0.0:
                dim = output.local_target.shape[-1]
                s_logits = output.local_target.reshape(-1, dim)  # (bs * seq_length, dim)
                t_logits = output.remote_target.detach().reshape(-1, dim)  # (bs * seq_length, dim)

            if self.alpha_mse > 0.0:
                if batch_weights is not None:
                    output.loss_mse = nn.MSELoss(reduction='none')(s_logits, t_logits) / s_logits.shape[0]
                    output.loss_mse = (batch_weights * output.loss_mse.mean(dim=(1, 2))).sum()
                else:
                    output.loss_mse = self.mse_loss_fct(s_logits, t_logits) / s_logits.shape[0]  # Reproducing batchmean reduction
            else:
                output.loss_mse = torch.tensor(0.)

            if self.alpha_ce > 0.0:
                dim = output.local_target.shape[-1]
                s_logits = output.local_target.reshape(-1, dim)  # (bs * seq_length, dim)
                t_logits = output.remote_target.detach().reshape(-1, dim)  # (bs * seq_length, dim)

                if batch_weights is not None:
                    output.loss_ce = (
                            nn.KLDivLoss(reduction='none')(
                                nn.functional.log_softmax(s_logits / self.temperature, dim=-1),
                                nn.functional.softmax(t_logits / self.temperature, dim=-1),
                            )
                            * (self.temperature) ** 2
                    )
                    output.loss_ce = (batch_weights * output.loss_ce.mean(dim=(1, 2))).sum()
                else:

                    output.loss_ce = (
                            self.ce_loss_fct(
                                nn.functional.log_softmax(s_logits / self.temperature, dim=-1),
                                nn.functional.softmax(t_logits / self.temperature, dim=-1),
                            )
                            * (self.temperature) ** 2
                    )
            else:
                output.loss_ce = torch.tensor(0.)

            if self.alpha_clm_dis > 0.0:
                if batch_weights is not None:
                    output.loss_clm_dis = nn.CrossEntropyLoss(reduction='none')(s_logits, nn.functional.softmax(t_logits, dim=-1))
                    output.loss_clm_dis = (batch_weights * output.loss_clm_dis.mean(dim=(1, 2))).sum()
                else:
                    output.loss_clm_dis = self.lm_loss_fct(s_logits, nn.functional.softmax(t_logits, dim=-1))
            else:
                output.loss_clm_dis = torch.tensor(0.)

        torch.cuda.empty_cache()  # to help clear GPU memory to prevent OOM errors

        return output

    def remote(self, inputs: torch.int64) -> torch.float32:
        """ Forwards the inputs through the network, selects the topk peers based on self.peer_weights.
        Args:
            inputs (:obj:`torch.int64` of shape :obj:`(batch_size, sequence_len)`, `required`):
                Batch_size length list of text sentences.
        Returns:
            outputs (:obj:`torch.FloatTensor` of shape :obj:`(batch_size, sequence_len, bittensor.__network_dim__)`, `optional`):
                Joined hidden layer responses from peers.
        """

        # ---- Get active peers and their weights ----
        active_uids = torch.where(self.metagraph().active > 0)[0]
        active_peer_weights = self.peer_weights[active_uids]

        # ---- Topk Weights ---- (TODO: check if the gaussians are enough disrupt the chain weights)
        real_topk = min(self.config.nucleus.topk, self.metagraph().n.item(), len(active_uids))
        std = torch.std(active_peer_weights).item() if torch.std(active_peer_weights).item() else self.noise_offset
        noise = torch.normal(0, std, size=(active_peer_weights.size())).to(self.config.neuron.device)
        topk_weights, topk_idx = torch.topk(active_peer_weights + noise, real_topk, dim=0)
        topk_uids = active_uids[topk_idx]

        # ---- Filter endpoints ----
        endpoints = self.metagraph().endpoints[topk_uids]

        # ---- Query network ----
        responses, return_ops, query_times = self.dendrite.forward_text(
            endpoints=endpoints.to('cpu'),
            inputs=inputs
        )

        # ---- Join based on weights ----
        joining_uids = torch.where(return_ops == bittensor.proto.ReturnCode.Success)[0]
        joining_weights = F.softmax(topk_weights[(return_ops == bittensor.proto.ReturnCode.Success)], dim=0)
        output = torch.zeros((inputs.shape[0], inputs.shape[1], bittensor.__network_dim__)).to(
            self.config.neuron.device)
        for index, joining_weight in enumerate(joining_weights):
            output += responses[joining_uids[index]].to(self.config.neuron.device) * joining_weight

        # ---- Punish peers with non-successful return ops ----
        with torch.no_grad():
            self.peer_weights[
                topk_uids[(return_ops != bittensor.proto.ReturnCode.Success)]] -= self.config.nucleus.punishment
            self.peer_weights[self.peer_weights < -1] = -1  # lower bound for chain weights

        return output
