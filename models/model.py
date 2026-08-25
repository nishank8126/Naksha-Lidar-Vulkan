"""
model.py — v3
==============
Compatible with ALL PyTorch versions (no custom_fwd decorator).
Forces fp32 in distance computations via autocast(enabled=False).
"""

import torch
import torch.nn as nn
import torch.nn.functional as F

def square_distance(src, dst):
    """Pairwise squared distance. FORCED fp32. Clamped non-negative."""
    with torch.amp.autocast('cuda', enabled=False):
        src = src.float()
        dst = dst.float()
        dist = (
            torch.sum(src ** 2, dim=-1, keepdim=True) +
            torch.sum(dst ** 2, dim=-1, keepdim=True).transpose(1, 2) -
            2 * torch.matmul(src, dst.transpose(1, 2))
        )
        return torch.clamp(dist, min=0.0)

def farthest_point_sample(xyz, npoint):
    """Farthest Point Sampling. fp32."""
    device = xyz.device
    B, N, _ = xyz.shape
    
    centroids = torch.zeros(B, npoint, dtype=torch.long, device=device)
    distance = torch.full((B, N), 1e10, dtype=torch.float32, device=device)
    
    farthest = torch.randint(0, N, (B,), dtype=torch.long, device=device)
    batch_idx = torch.arange(B, dtype=torch.long, device=device)
    
    with torch.amp.autocast('cuda', enabled=False):
        xyz_f32 = xyz.float()
        for i in range(npoint):
            centroids[:, i] = farthest
            centroid = xyz_f32[batch_idx, farthest, :].unsqueeze(1)
            dist = torch.sum((xyz_f32 - centroid) ** 2, dim=-1)
            distance = torch.min(distance, dist)
            farthest = torch.max(distance, dim=-1)[1]
    
    return centroids


# query_ball_point — REPLACE entire function:
def query_ball_point(radius, nsample, xyz, new_xyz):
    """Ball query — memory-optimized with topk."""
    device = xyz.device
    B, N, _ = xyz.shape
    _, S, _ = new_xyz.shape

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
    """Gather points by index."""
    device = points.device
    B = points.shape[0]
    view_shape = list(idx.shape)
    view_shape[1:] = [1] * (len(view_shape) - 1)
    repeat_shape = list(idx.shape)
    repeat_shape[0] = 1
    batch_indices = torch.arange(
        B, dtype=torch.long, device=device
    ).view(view_shape).repeat(repeat_shape)
    return points[batch_indices, idx, :]


class SetAbstraction(nn.Module):
    def __init__(self, npoint, radius, nsample, in_channel, mlp_channels):
        super().__init__()
        self.npoint = npoint
        self.radius = radius
        self.nsample = nsample
        
        self.convs = nn.ModuleList()
        self.bns = nn.ModuleList()
        last_ch = in_channel + 3
        for out_ch in mlp_channels:
            self.convs.append(nn.Conv2d(last_ch, out_ch, 1, bias=False))
            self.bns.append(nn.BatchNorm2d(out_ch))
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
        
        for conv, bn in zip(self.convs, self.bns):
            grouped = F.relu(bn(conv(grouped)))
        
        new_features = torch.max(grouped, dim=2)[0]
        return new_xyz, new_features.permute(0, 2, 1)


class FeaturePropagation(nn.Module):
    def __init__(self, in_channel, mlp_channels):
        super().__init__()
        self.convs = nn.ModuleList()
        self.bns = nn.ModuleList()
        last_ch = in_channel
        for out_ch in mlp_channels:
            self.convs.append(nn.Conv1d(last_ch, out_ch, 1, bias=False))
            self.bns.append(nn.BatchNorm1d(out_ch))
            last_ch = out_ch
    
    def forward(self, xyz_target, xyz_source, feat_target, feat_source):
        B, N, _ = xyz_target.shape
        _, S, _ = xyz_source.shape
        
        if S == 1:
            interpolated = feat_source.expand(-1, N, -1)
        else:
            dists = square_distance(xyz_target, xyz_source)
            dists, idx = dists.sort(dim=-1)
            dists = dists[:, :, :3]
            idx = idx[:, :, :3]
            
            with torch.amp.autocast('cuda', enabled=False):
                dist_recip = 1.0 / (dists.float() + 1e-8)
                weight = dist_recip / dist_recip.sum(dim=2, keepdim=True)
                gathered = index_points(feat_source, idx).float()
                interpolated = torch.sum(
                    gathered * weight.unsqueeze(-1), dim=2
                )
        
        if feat_target is not None:
            new_features = torch.cat([feat_target, interpolated], dim=-1)
        else:
            new_features = interpolated
        
        new_features = new_features.permute(0, 2, 1)
        for conv, bn in zip(self.convs, self.bns):
            new_features = F.relu(bn(conv(new_features)))
        return new_features.permute(0, 2, 1)


class PointNet2SSG(nn.Module):
    def __init__(self, num_features=66, num_classes=5):
        super().__init__()
        self.num_classes = num_classes
        self.num_features = num_features
        
        self.input_mlp = nn.Sequential(
            nn.Conv1d(num_features, 32, 1, bias=False),  # ◄ Conv1d, not Linear
            nn.BatchNorm1d(32),
            nn.ReLU(inplace=True),
        )
        
        self.sa1 = SetAbstraction(2048, 1.0, 32, 32, [32, 32, 64])
        self.sa2 = SetAbstraction(512, 2.0, 32, 64, [64, 64, 128])
        self.sa3 = SetAbstraction(128, 5.0, 32, 128, [128, 128, 256])
        self.sa4 = SetAbstraction(32, 10.0, 32, 256, [256, 256, 512])
        
        self.fp4 = FeaturePropagation(512 + 256, [256, 256])
        self.fp3 = FeaturePropagation(256 + 128, [256, 128])
        self.fp2 = FeaturePropagation(128 + 64, [128, 64])
        self.fp1 = FeaturePropagation(64 + 32, [64, 64])
        
        self.head = nn.Sequential(
            nn.Conv1d(64, 64, 1, bias=False),
            nn.BatchNorm1d(64),
            nn.ReLU(inplace=True),
            nn.Dropout(0.5),
            nn.Conv1d(64, num_classes, 1),
        )
    
    def forward(self, coords, features):
        B, N, _ = coords.shape
        feat_proj = self.input_mlp(features.permute(0, 2, 1))  # (B,F,N) → (B,32,N)
        feat_proj = feat_proj.permute(0, 2, 1)                   # (B,N,32)
        
        l0_xyz, l0_feat = coords, feat_proj
        
        l1_xyz, l1_feat = self.sa1(l0_xyz, l0_feat)
        l2_xyz, l2_feat = self.sa2(l1_xyz, l1_feat)
        l3_xyz, l3_feat = self.sa3(l2_xyz, l2_feat)
        l4_xyz, l4_feat = self.sa4(l3_xyz, l3_feat)
        
        l3_feat = self.fp4(l3_xyz, l4_xyz, l3_feat, l4_feat)
        l2_feat = self.fp3(l2_xyz, l3_xyz, l2_feat, l3_feat)
        l1_feat = self.fp2(l1_xyz, l2_xyz, l1_feat, l2_feat)
        l0_feat = self.fp1(l0_xyz, l1_xyz, l0_feat, l1_feat)
        
        x = l0_feat.permute(0, 2, 1)
        logits = self.head(x)
        return logits.permute(0, 2, 1)


def count_parameters(model):
    total = sum(p.numel() for p in model.parameters() if p.requires_grad)
    mb = sum(p.numel() * p.element_size() for p in model.parameters()) / 1e6
    print(f"  Parameters: {total:,} ({mb:.1f} MB)")
    return total


def estimate_memory(num_points=8192, batch_size=4, num_features=66):
    total = batch_size * num_points * 128 * 2 / 1e6 * 4 + 500
    print(f"  VRAM estimate: ~{total:.0f} MB ({total/1024:.1f} GB)")
    return total