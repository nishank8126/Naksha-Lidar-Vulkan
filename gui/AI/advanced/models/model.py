"""
model.py — v4_pro
=================
Production-oriented PointNet++ style model for 46-feature LiDAR segmentation.

Goals:
- stronger than the current PointNet2SSG
- still safe on 4 GB GPU
- compatible with existing train/evaluate/inference signatures:
    model(coords, features) -> logits (B, N, num_classes)
"""

import torch
import torch.nn as nn
import torch.nn.functional as F


# ============================================================
# CORE GEOMETRY UTILS
# ============================================================

def square_distance(src, dst):
    with torch.amp.autocast("cuda", enabled=False):
        src = src.float()
        dst = dst.float()
        dist = (
            torch.sum(src ** 2, dim=-1, keepdim=True)
            + torch.sum(dst ** 2, dim=-1, keepdim=True).transpose(1, 2)
            - 2 * torch.matmul(src, dst.transpose(1, 2))
        )
        return torch.clamp(dist, min=0.0)


def farthest_point_sample(xyz, npoint):
    device = xyz.device
    B, N, _ = xyz.shape

    centroids = torch.zeros(B, npoint, dtype=torch.long, device=device)
    distance = torch.full((B, N), 1e10, dtype=torch.float32, device=device)

    farthest = torch.randint(0, N, (B,), dtype=torch.long, device=device)
    batch_idx = torch.arange(B, dtype=torch.long, device=device)

    with torch.amp.autocast("cuda", enabled=False):
        xyz_f32 = xyz.float()
        for i in range(npoint):
            centroids[:, i] = farthest
            centroid = xyz_f32[batch_idx, farthest, :].unsqueeze(1)
            dist = torch.sum((xyz_f32 - centroid) ** 2, dim=-1)
            distance = torch.min(distance, dist)
            farthest = torch.max(distance, dim=-1)[1]

    return centroids


def query_ball_point(radius, nsample, xyz, new_xyz):
    sqrdists = square_distance(new_xyz, xyz)
    radius_sq = radius ** 2

    sqrdists_masked = sqrdists.clone()
    sqrdists_masked[sqrdists > radius_sq] = 1e10

    _, group_idx = torch.topk(
        sqrdists_masked, nsample, dim=-1, largest=False, sorted=False
    )

    first_idx = group_idx[:, :, 0:1].expand_as(group_idx)
    gathered_dists = torch.gather(sqrdists, 2, group_idx)
    outside_mask = gathered_dists > radius_sq
    group_idx[outside_mask] = first_idx[outside_mask]

    return group_idx


def index_points(points, idx):
    device = points.device
    B = points.shape[0]

    view_shape = list(idx.shape)
    view_shape[1:] = [1] * (len(view_shape) - 1)

    repeat_shape = list(idx.shape)
    repeat_shape[0] = 1

    batch_indices = (
        torch.arange(B, dtype=torch.long, device=device)
        .view(view_shape)
        .repeat(repeat_shape)
    )
    return points[batch_indices, idx, :]


# ============================================================
# SMALL BUILDING BLOCKS
# ============================================================

class ConvBNReLU1d(nn.Module):
    def __init__(self, in_ch, out_ch, p=0.0):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv1d(in_ch, out_ch, 1, bias=False),
            nn.BatchNorm1d(out_ch),
            nn.ReLU(inplace=True),
            nn.Dropout(p) if p > 0 else nn.Identity(),
        )

    def forward(self, x):
        return self.net(x)


class ConvBNReLU2d(nn.Module):
    def __init__(self, in_ch, out_ch):
        super().__init__()
        self.net = nn.Sequential(
            nn.Conv2d(in_ch, out_ch, 1, bias=False),
            nn.BatchNorm2d(out_ch),
            nn.ReLU(inplace=True),
        )

    def forward(self, x):
        return self.net(x)


class ResidualMLP1d(nn.Module):
    def __init__(self, ch, p=0.0):
        super().__init__()
        self.block = nn.Sequential(
            nn.Conv1d(ch, ch, 1, bias=False),
            nn.BatchNorm1d(ch),
            nn.ReLU(inplace=True),
            nn.Dropout(p) if p > 0 else nn.Identity(),
            nn.Conv1d(ch, ch, 1, bias=False),
            nn.BatchNorm1d(ch),
        )
        self.act = nn.ReLU(inplace=True)

    def forward(self, x):
        return self.act(x + self.block(x))


class SE1d(nn.Module):
    def __init__(self, ch, reduction=8):
        super().__init__()
        hidden = max(ch // reduction, 8)
        self.pool = nn.AdaptiveAvgPool1d(1)
        self.fc = nn.Sequential(
            nn.Conv1d(ch, hidden, 1),
            nn.ReLU(inplace=True),
            nn.Conv1d(hidden, ch, 1),
            nn.Sigmoid(),
        )

    def forward(self, x):
        w = self.fc(self.pool(x))
        return x * w


# ============================================================
# SET ABSTRACTION / FEATURE PROP
# ============================================================

class SetAbstraction(nn.Module):
    def __init__(self, npoint, radius, nsample, in_channel, mlp_channels):
        super().__init__()
        self.npoint = npoint
        self.radius = radius
        self.nsample = nsample

        self.convs = nn.ModuleList()
        last_ch = in_channel + 3
        for out_ch in mlp_channels:
            self.convs.append(ConvBNReLU2d(last_ch, out_ch))
            last_ch = out_ch

    def forward(self, xyz, features):
        fps_idx = farthest_point_sample(xyz, self.npoint)
        new_xyz = index_points(xyz, fps_idx)

        idx = query_ball_point(self.radius, self.nsample, xyz, new_xyz)

        grouped_xyz = index_points(xyz, idx)
        grouped_xyz -= new_xyz.unsqueeze(2)

        grouped_feat = index_points(features, idx)
        grouped = torch.cat([grouped_xyz, grouped_feat], dim=-1)
        grouped = grouped.permute(0, 3, 2, 1)

        for conv in self.convs:
            grouped = conv(grouped)

        new_features = torch.max(grouped, dim=2)[0]
        return new_xyz, new_features.permute(0, 2, 1)


class FeaturePropagation(nn.Module):
    def __init__(self, in_channel, mlp_channels):
        super().__init__()
        layers = []
        last_ch = in_channel
        for out_ch in mlp_channels:
            layers.append(ConvBNReLU1d(last_ch, out_ch))
            last_ch = out_ch
        self.layers = nn.Sequential(*layers)

    def forward(self, xyz_target, xyz_source, feat_target, feat_source):
        _, N, _ = xyz_target.shape
        _, S, _ = xyz_source.shape

        if S == 1:
            interpolated = feat_source.expand(-1, N, -1)
        else:
            dists = square_distance(xyz_target, xyz_source)
            dists, idx = dists.sort(dim=-1)
            dists = dists[:, :, :3]
            idx = idx[:, :, :3]

            with torch.amp.autocast("cuda", enabled=False):
                dist_recip = 1.0 / (dists.float() + 1e-8)
                weight = dist_recip / dist_recip.sum(dim=2, keepdim=True)
                gathered = index_points(feat_source, idx).float()
                interpolated = torch.sum(gathered * weight.unsqueeze(-1), dim=2)

        if feat_target is not None:
            new_features = torch.cat([feat_target, interpolated], dim=-1)
        else:
            new_features = interpolated

        new_features = new_features.permute(0, 2, 1)
        new_features = self.layers(new_features)
        return new_features.permute(0, 2, 1)


# ============================================================
# PRODUCTION MODEL
# ============================================================

class PointNet2SSGPro(nn.Module):
    def __init__(self, num_features=68, num_classes=5):
        super().__init__()
        self.num_classes = num_classes
        self.num_features = num_features

        # Stronger input stem than current 32-only projection
        self.input_stem = nn.Sequential(
            ConvBNReLU1d(num_features, 48, p=0.05),
            ResidualMLP1d(48, p=0.05),
        )

        # Slightly stronger hierarchy, still 4GB-safe
        self.sa1 = SetAbstraction(2048, 1.0, 32, 48, [48, 64, 96])
        self.sa2 = SetAbstraction(512, 2.0, 32, 96, [96, 128, 160])
        self.sa3 = SetAbstraction(128, 5.0, 32, 160, [160, 192, 256])
        self.sa4 = SetAbstraction(32, 10.0, 32, 256, [256, 320, 512])

        self.fp4 = FeaturePropagation(512 + 256, [320, 256])
        self.fp3 = FeaturePropagation(256 + 160, [256, 192])
        self.fp2 = FeaturePropagation(192 + 96, [160, 128])
        self.fp1 = FeaturePropagation(128 + 48, [128, 96])

        self.refine = nn.Sequential(
            ConvBNReLU1d(96, 96, p=0.10),
            ResidualMLP1d(96, p=0.10),
            SE1d(96, reduction=8),
        )

        self.head = nn.Sequential(
            ConvBNReLU1d(96, 96, p=0.15),
            SE1d(96, reduction=8),
            nn.Conv1d(96, 64, 1, bias=False),
            nn.BatchNorm1d(64),
            nn.ReLU(inplace=True),
            nn.Dropout(0.25),
            nn.Conv1d(64, num_classes, 1),
        )

    def forward(self, coords, features):
        feat0 = self.input_stem(features.permute(0, 2, 1))
        feat0 = feat0.permute(0, 2, 1)

        l0_xyz, l0_feat = coords, feat0

        l1_xyz, l1_feat = self.sa1(l0_xyz, l0_feat)
        l2_xyz, l2_feat = self.sa2(l1_xyz, l1_feat)
        l3_xyz, l3_feat = self.sa3(l2_xyz, l2_feat)
        l4_xyz, l4_feat = self.sa4(l3_xyz, l3_feat)

        l3_feat = self.fp4(l3_xyz, l4_xyz, l3_feat, l4_feat)
        l2_feat = self.fp3(l2_xyz, l3_xyz, l2_feat, l3_feat)
        l1_feat = self.fp2(l1_xyz, l2_xyz, l1_feat, l2_feat)
        l0_feat = self.fp1(l0_xyz, l1_xyz, l0_feat, l1_feat)

        x = l0_feat.permute(0, 2, 1)
        x = self.refine(x)
        logits = self.head(x)
        return logits.permute(0, 2, 1)


def count_parameters(model):
    total = sum(p.numel() for p in model.parameters() if p.requires_grad)
    mb = sum(p.numel() * p.element_size() for p in model.parameters()) / 1e6
    print(f"  Parameters: {total:,} ({mb:.1f} MB)")
    return total


def estimate_memory(num_points=8192, batch_size=1, num_features=68):
    total = batch_size * num_points * 160 * 2 / 1e6 * 4 + 650
    print(f"  VRAM estimate: ~{total:.0f} MB ({total / 1024:.1f} GB)")
    return total