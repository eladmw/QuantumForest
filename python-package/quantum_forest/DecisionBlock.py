import torch
import torch.nn as nn
import torch.nn.functional as F
import node_lib
import quantum_forest
from node_lib.odst import ODST
import copy
import random
from .sparse_max import sparsemax, sparsemoid, entmoid15,entmax15

class DecisionBlock(nn.Sequential):
    def __init__(self, input_dim, config, flatten_output=True, feat_info=None, **kwargs):
        super(DecisionBlock, self).__init__()
        self.config = config
        layers = []
        tree_dim=config.tree_dim
        num_trees = config.nTree
        Module = config.tree_module
        for i in range(config.num_layers):
            layer = Module(input_dim, num_trees, config, flatten_output=True,feat_info=feat_info, **kwargs)
            input_dim = min(input_dim + num_trees * tree_dim, config.max_features or float('inf'))
            layers.append(layer)

        super().__init__(*layers)
        self.num_layers, self.layer_dim, self.tree_dim = config.num_layers, num_trees, tree_dim
        self.max_features, self.flatten_output = config.max_features, flatten_output
        self.input_dropout = config.input_dropout

    def get_attentions(self):
        attentions=[]
        for layer in self:
            attentions.append(layer.feat_attention)
        return attentions

    def forward(self, x):
        nSamp = x.shape[0]
        initial_features = x.shape[-1]
        for layer in self:
            layer_inp = x
            if self.max_features is not None:
                tail_features = min(self.max_features, layer_inp.shape[-1]) - initial_features
                if tail_features != 0:
                    layer_inp = torch.cat([layer_inp[..., :initial_features], layer_inp[..., -tail_features:]], dim=-1)
            if self.training and self.input_dropout:
                layer_inp = F.dropout(layer_inp, self.input_dropout)
            h = layer(layer_inp)
            x = torch.cat([x, h], dim=-1)

        outputs = x[..., initial_features:]
        if not self.flatten_output:
            outputs = outputs.view(*outputs.shape[:-1], self.num_layers * self.layer_dim, self.tree_dim)
        if self.config.max_out:
            outputs = torch.max(outputs, -1).values
        else:
            outputs = outputs[..., 0]

        #outputs = torch.mean(outputs, -1)      确实不如maxout
        #outputs = outputs.mean(dim=-1)
        return outputs

    def AfterEpoch(self,epoch=0):
        pass

class MultiBlock(nn.Module):
    def __init__(self, input_dim, config_0, flatten_output=True, feat_info=None, **kwargs):
        super(MultiBlock, self).__init__()
        self.in_features = input_dim
        self.nSub = 10
        self.nEachTree = config_0.nTree //10
        self.blocks=nn.ModuleList()
        self.isSparseFeat=False
        # 效果不明显，很难替换    [3.2853165 4.8626194 3.4907362 3.6596875 3.849822  4.1120253 4.332555,3.3396778 4.4621625 2.7200086]
        self.block_weight = nn.Parameter(torch.Tensor(self.nSub).uniform_(), requires_grad=True)
        if self.isSparseFeat:
            self.nEachFeat = input_dim//2
        else:
            self.nEachFeat = input_dim
        self.feat_maps=[]
        self.feat_W = []
        for i in range(self.nSub):
            config = copy.deepcopy(config_0)
            config.nTree = self.nEachTree
            nFeat = self.nEachFeat
            if self.isSparseFeat:
                map = random.choices(population = list(range(self.in_features)),k = nFeat)
                self.feat_maps.append(map)
                sub_info = feat_info.iloc[map, :]
            else:
                self.feat_W.append(nn.Parameter(torch.Tensor(self.in_features).uniform_().cuda(), requires_grad=True))
                sub_info = feat_info
            block = DecisionBlock(nFeat, config, flatten_output=flatten_output,feat_info=sub_info)
            self.blocks.append(block)

        print(f"====== MultiBlock nSub={self.nSub} nEachTree={self.nEachTree} nEachFeat={self.nEachFeat}")

    def forward(self, x00):
        outputs=[]
        for i,block in enumerate(self.blocks):
            if self.isSparseFeat:
                map = self.feat_maps[i]
                x0=x00[:,map]
            else:
                x0 = x00
                #feat_w = entmax15(self.feat_W[i], dim=0)
                #feat_w = self.feat_W[i]
                #x0 = torch.einsum('bf,f->bf', x00, feat_w)
            x=block.forward(x0)
            outputs.append(x*self.block_weight[i])
        output = torch.cat(outputs,dim=1)
        return output

    def AfterEpoch(self,epoch=0):
        print(f"\t==== block_weight={self.block_weight.detach().cpu().numpy()}")