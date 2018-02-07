import torch
from torch import nn
import matchbox
from matchbox import functional as F
from matchbox.data import MaskedBatchField

import math

def positional_encodings_like(x, t=None):
    T, D = x.maxsize(-2), x.maxsize(-1)
    positions = torch.arange(0, T, out=x.new(T)) if t is None else t
    channels = torch.arange(0, D, 2, out=x.new(D)) / D
    channels = 1 / (10000 ** channels)
    encodings = positions.unsqueeze(-1) @ channels.unsqueeze(0)
    encodings = torch.stack((encodings.sin(), encodings.cos()), -1)
    encodings = encodings.contiguous().view(*encodings.size()[:-2], -1)

    if encodings.dim() == 2:
        encodings = encodings.unsqueeze(0).expand(x.maxsize())

    return encodings

class LayerNorm(nn.Module):

    def __init__(self, d_model, eps=1e-6):
        super().__init__()
        self.gamma = nn.Parameter(torch.ones(d_model))
        self.beta = nn.Parameter(torch.zeros(d_model))
        self.eps = eps

    def forward(self, x):
        mean = x.mean(-1, keepdim=True)
        std = x.std(-1, keepdim=True)
        return self.gamma * (x - mean) / (std + self.eps) + self.beta

class FeedForward(nn.Module):

    def __init__(self, d_model, d_hidden, drop_ratio):
        super().__init__()
        self.linear1 = nn.Linear(d_model, d_hidden)
        self.linear2 = nn.Linear(d_hidden, d_model)
        self.dropout = nn.Dropout(drop_ratio)

    def forward(self, x):
        return self.linear2(self.dropout(F.relu(self.linear1(x))))

class ResidualBlock(nn.Module):

    def __init__(self, layer, d_model, drop_ratio):
        super().__init__()
        self.layer = layer
        self.dropout = nn.Dropout(drop_ratio)
        self.norm = LayerNorm(d_model)

    def forward(self, *x):
        return x[0] + self.dropout(self.norm(self.layer(*x)))

class Attention(nn.Module):

    def __init__(self, d_key, drop_ratio, causal=False):
        super().__init__()
        self.scale = math.sqrt(d_key)
        self.dropout = nn.Dropout(drop_ratio)
        self.causal = causal

    def forward(self, query, key, value):
        dot_products = query @ key.transpose(1, 2)

        if self.causal and query.dim() == 3:
            dot_products = dot_products.causal_mask(in_dim=2, out_dim=1)

        return self.dropout((dot_products / self.scale).softmax()) @ value

class MultiHead(nn.Module):

    def __init__(self, attention, d_key, d_value, n_heads):
        super().__init__()
        self.attention = attention
        self.wq = nn.Linear(d_key, d_key)
        self.wk = nn.Linear(d_key, d_key)
        self.wv = nn.Linear(d_value, d_value)
        self.wo = nn.Linear(d_value, d_key)
        self.n_heads = n_heads

    def forward(self, query, key, value):
        query, key, value = self.wq(query), self.wk(key), self.wv(value)
        # B x T x D -> B x T x (D/N) x N -> (B*N) x T x (D/N)
        query, key, value = (x.split_dim(-1, self.n_heads).combine_dims(0, -1)
                             for x in (query, key, value))
        outputs = self.attention(query, key, value)
        # (B*N) x T x (D/N) -> B x N x T x (D/N) -> B x T x D
        outputs = outputs.split_dim(0, self.n_heads).combine_dims(-1, 1)
        return self.wo(outputs)

class EncoderLayer(nn.Module):

    def __init__(self, args):
        super().__init__()
        self.selfattn = ResidualBlock(
            MultiHead(Attention(args.d_model, args.drop_ratio),
                args.d_model, args.d_model, args.n_heads),
            args.d_model, args.drop_ratio)
        self.feedforward = ResidualBlock(
            FeedForward(args.d_model, args.d_hidden, args.drop_ratio),
            args.d_model, args.drop_ratio)

    def forward(self, x):
        x = self.selfattn(x, x, x)
        return self.feedforward(x)

class DecoderLayer(nn.Module):

    def __init__(self, args):
        super().__init__()
        self.selfattn = ResidualBlock(
            MultiHead(Attention(args.d_model, args.drop_ratio, True),
                      args.d_model, args.d_model, args.n_heads),
            args.d_model, args.drop_ratio)

        self.attention = ResidualBlock(
            MultiHead(Attention(args.d_model, args.drop_ratio),
                      args.d_model, args.d_model, args.n_heads),
            args.d_model, args.drop_ratio)

        self.feedforward = ResidualBlock(
            FeedForward(args.d_model, args.d_hidden, args.drop_ratio),
            args.d_model, args.drop_ratio)

    def forward(self, x, encoding):
        x = self.selfattn(x, x, x)
        x = self.attention(x, encoding, encoding)
        return self.feedforward(x)

class Encoder(nn.Module):

    def __init__(self, field, args):
        super().__init__()
        self.out = field.out
        self.layers = nn.ModuleList(
            [EncoderLayer(args) for i in range(args.n_layers)])
        self.dropout = nn.Dropout(args.drop_ratio)
        self.field = field
        self.d_model = args.d_model

    def forward(self, x):
        x = F.embedding(x, self.out.weight * math.sqrt(self.d_model))
        x += positional_encodings_like(x)
        x = self.dropout(x)
        encoding = []
        for layer in self.layers:
            x = layer(x)
            encoding.append(x)
        return encoding

class Decoder(nn.Module):

    def __init__(self, field, args):
        super().__init__()
        self.out = field.out
        self.layers = nn.ModuleList(
            [DecoderLayer(args) for i in range(args.n_layers)])
        self.dropout = nn.Dropout(args.drop_ratio)
        self.d_model = args.d_model
        self.field = field
        self.length_ratio = args.length_ratio

    def forward(self, x, encoding):
        x = F.embedding(x, self.out.weight * math.sqrt(self.d_model))
        x += positional_encodings_like(x)
        x = self.dropout(x)

        for l, (layer, enc) in enumerate(zip(self.layers, encoding)):
            x = layer(x, enc)
        return x

class Transformer(nn.Module):

    def __init__(self, src, trg, args):
        super().__init__()
        for field in set((src, trg)):
            field.out = nn.Linear(args.d_model, len(field.vocab))
        self.encoder = Encoder(src, args)
        self.decoder = Decoder(trg, args)

    def forward(self, encoder_inputs, decoder_inputs, decoding=False, beam=1,
                alpha=0.6, return_probs=False):
        encoding = self.encoder(encoder_inputs)

        if (return_probs and decoding) or (not decoding):
            out = self.decoder(decoder_inputs, encoding)

        if decoding:
            if beam == 1:
                output = self.decoder.greedy(encoding)
            else:
                output = self.decoder.beam_search(encoding, beam, alpha)

            if return_probs:
                return output, out, self.decoder.out(out).softmax()
            return output

        if return_probs:
            return out, F.softmax(self.decoder.out(out))
        return out
