# Copyright (c) 2023 Graphcore Ltd. All rights reserved.

import os

dataset_directory = os.getenv("DATASET_DIR", "data")

import torch_geometric.transforms as T
from torch_geometric.datasets import Planetoid

dataset = Planetoid(dataset_directory, "Cora", transform=T.NormalizeFeatures())
data = dataset[0]
print(data)

import torch
import torch.nn.functional as F
from torch_geometric.nn import GCNConv


class GCN(torch.nn.Module):
    def __init__(self, in_channels, out_channels):
        super().__init__()
        torch.manual_seed(1234)
        self.conv = GCNConv(in_channels, out_channels, add_self_loops=False)

    def forward(self, x, edge_index, edge_weight=None):
        x = F.dropout(x, p=0.5, training=self.training)
        x = self.conv(x, edge_index, edge_weight).relu()
        return x


model = GCN(dataset.num_features, dataset.num_classes)
model.train()
optimizer = torch.optim.Adam(model.parameters(), lr=0.001)

print("Training on CPU.")

for epoch in range(1, 6):
    optimizer.zero_grad()
    out = model(data.x, data.edge_index, data.edge_attr)
    loss = F.cross_entropy(out, data.y)
    loss.backward()
    optimizer.step()
    print(f"Epoch: {epoch}, Loss: {loss}")

import poptorch


class GCN(torch.nn.Module):
    def __init__(self, in_channels, out_channels):
        super().__init__()
        torch.manual_seed(1234)
        self.conv = GCNConv(in_channels, out_channels, add_self_loops=False)

    def forward(self, x, edge_index, y, edge_weight=None):
        x = F.dropout(x, p=0.5, training=self.training)
        x = self.conv(x, edge_index, edge_weight).relu()

        if self.training:
            loss = F.cross_entropy(x, y)
            return x, loss
        return x


model = GCN(dataset.num_features, dataset.num_classes)
model.train()
optimizer = poptorch.optim.Adam(model.parameters(), lr=0.001)
poptorch_model = poptorch.trainingModel(model, optimizer=optimizer)

print("Training on IPU.")
for epoch in range(1, 6):
    output, loss = poptorch_model(data.x, data.edge_index, data.y, edge_weight=data.edge_attr)
    print(f"Epoch: {epoch}, Loss: {loss}")

from torch_geometric.datasets import TUDataset

dataset = TUDataset(dataset_directory, name="MUTAG")
data = dataset[0]
print(data)

from torch_geometric.loader import DataLoader

torch.manual_seed(1234)
dataloader = DataLoader(dataset, batch_size=10, shuffle=True)

from poptorch_geometric import FixedSizeDataLoader
from torch_geometric.data.summary import Summary

torch.manual_seed(1234)

dataset_summary = Summary.from_dataset(dataset)
dataset_summary
max_number_of_nodes = int(dataset_summary.num_nodes.max)
max_number_of_edges = int(dataset_summary.num_edges.max)
print(f"Max number of nodes in the dataset is: {max_number_of_nodes}")
print(f"Max number of edges in the dataset is: {max_number_of_edges}")

ipu_dataloader = FixedSizeDataLoader(dataset, num_nodes=300, num_edges=600, batch_size=10)

print(f"{next(iter(dataloader)) = }")
print(f"{next(iter(ipu_dataloader)) = }")

from torch_geometric.nn import global_mean_pool


class GcnForIpu(torch.nn.Module):
    def __init__(self, in_channels, out_channels, batch_size):
        super().__init__()
        torch.manual_seed(1234)
        self.batch_size = batch_size
        self.conv = GCNConv(in_channels, out_channels, add_self_loops=False)

    def forward(self, x, edge_index, y, batch):
        x = self.conv(x, edge_index).relu()

        x = global_mean_pool(x, batch, size=self.batch_size)

        if self.training:
            loss = F.cross_entropy(x, y)
            return x, loss

        return x


model = GcnForIpu(dataset.num_features, dataset.num_classes, batch_size=10)

optim = poptorch.optim.Adam(model.parameters(), lr=0.01)
poptorch_model = poptorch.trainingModel(model, optimizer=optim)
poptorch_model.train()

in_data = next(iter(ipu_dataloader))
poptorch_model(in_data.x, in_data.edge_index, in_data.y, in_data.batch)

dataset = Planetoid(dataset_directory, "Cora", transform=T.NormalizeFeatures())
data = dataset[0]

x = data.x[data.train_mask]
y = data.y[data.train_mask]
loss = F.cross_entropy(x, y)

y = torch.where(data.train_mask, data.y, -100)
loss = F.cross_entropy(data.x, y)

from torch_geometric.nn import global_mean_pool

x = global_mean_pool(data.x, data.batch)

batch_size = 1
x = global_mean_pool(data.x, data.batch, size=batch_size)

conv = GCNConv(in_channels=10, out_channels=10)
conv

conv = GCNConv(in_channels=10, out_channels=10, add_self_loops=False)
conv

import torch_geometric.transforms as T

transform = T.AddSelfLoops()
transform

dataset = TUDataset(f"{dataset_directory}/self_loops", name="MUTAG", pre_transform=transform)
dataset
