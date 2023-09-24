import torch
import torch.nn as nn

import dinov2.distributed as distributed
from dinov2.eval.utils import is_zero_matrix

class DINOV2Encoder(torch.nn.Module):
    def __init__(self, encoder, autocast_ctx, is_3d=False) -> None:
        super(DINOV2Encoder, self).__init__()
        self.encoder = encoder
        self.encoder.eval()
        self.autocast_ctx = autocast_ctx
        self.is_3d = is_3d
    
    def forward_3d(self, x):
        batch_features = [] 
        for batch_scans in x: # calculate the features for every scan in all scans of the batch
            scans = []
            for scan in batch_scans:
                if not is_zero_matrix(scan): scans.append(self.forward_(scan.unsqueeze(0)))
            batch_features.append(scans)
        return batch_features

    def forward_(self, x):
        with torch.no_grad():
            with self.autocast_ctx():
                features = self.encoder.forward_features(x)['x_norm_patchtokens']
        return features

    def forward(self, x):
        if self.is_3d:
            return self.forward_3d(x)
        return self.forward_(x)

class LinearDecoder(torch.nn.Module):
    """Linear decoder head"""
    DECODER_TYPE = "linear"

    def __init__(self, in_channels, tokenW=32, tokenH=32, num_classes=3, is_3d=False):
        super().__init__()

        self.in_channels = in_channels
        self.width = tokenW
        self.height = tokenH
        self.decoder = torch.nn.Conv2d(in_channels, num_classes, (1,1))
        self.decoder.weight.data.normal_(mean=0.0, std=0.01)
        self.decoder.bias.data.zero_()
        self.is_3d = is_3d

    def forward_3d(self, embeddings, vectorized=False):
        batch_outputs = []
        for batch_embeddings in embeddings:
            if vectorized:
                batch_outputs.append(self.forward_(torch.stack(batch_embeddings).squeeze()))
            else:
                batch_outputs.append(
                    torch.stack([self.forward_(slice_embedding) for slice_embedding in batch_embeddings]).squeeze()
                    )
        return batch_outputs

    def forward_(self, embeddings):
        embeddings = embeddings.reshape(-1, self.height, self.width, self.in_channels)
        embeddings = embeddings.permute(0,3,1,2)

        # Upsample (interpolate) output/logit map. 
        output = self.decoder(embeddings)
        output = torch.nn.functional.interpolate(output, size=448, mode="bilinear", align_corners=False)

        return output
    
    def forward(self, embeddings):
        if self.is_3d:
            return self.forward_3d(embeddings)
        return self.forward_(embeddings)
    
class LinearPostprocessor(nn.Module):
    def __init__(self, decoder):
        super().__init__()
        self.decoder = decoder

    def forward(self, samples, targets):
        logits = self.decoder(samples) 
        if isinstance(logits, list): # if 3D output
            logits = torch.cat(logits, dim=0)
            targets = torch.cat(targets, dim=0).cuda()

        preds = logits.argmax(dim=1)
        targets = targets.type(torch.int64)

        return {
            "preds": preds,
            "target": targets,
        }

class AllDecoders(nn.Module):
    def __init__(self, decoders_dict):
        super().__init__()
        self.decoders_dict = nn.ModuleDict()
        self.decoders_dict.update(decoders_dict)
        self.decoder_type = list(decoders_dict.values())[0].DECODER_TYPE

    def forward(self, inputs):
        return {k: v.forward(inputs) for k, v in self.decoders_dict.items()}

    def __len__(self):
        return len(self.decoders_dict)

def setup_decoders(embed_dim, learning_rates, num_classes=14, decoder_type="linear", is_3d=False):
    """
    Sets up the multiple segmentors with different hyperparameters to test out the most optimal one 
    """
    decoders_dict = nn.ModuleDict()
    optim_param_groups = []
    for lr in learning_rates:
        if decoder_type == "linear":
            decoder = LinearDecoder(
                embed_dim, num_classes=num_classes, is_3d=is_3d
            )
        decoder = decoder.cuda()
        decoders_dict[
            f"{decoder_type}:lr={lr:.10f}".replace(".", "_")
        ] = decoder
        optim_param_groups.append({"params": decoder.parameters(), "lr": lr})

    decoders = AllDecoders(decoders_dict)
    if distributed.is_enabled():
        decoders = nn.parallel.DistributedDataParallel(decoders)

    return decoders, optim_param_groups