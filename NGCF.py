'''
Created on March 24, 2020

@author: Tinglin Huang (huangtinglin@outlook.com)
'''

import torch
import torch.nn as nn
import torch.nn.functional as F


class NGCF(nn.Module):

    def __init__(self, n_user, n_item, norm_adj):
        super(NGCF, self).__init__()
        self.n_user = n_user
        self.n_item = n_item
        self.device = torch.device('cuda')
        self.emb_size = 24
        self.batch_size = 512
        self.decay = 0
        self.node_dropout = 0
        self.mess_dropout = 0
        self.norm_adj = norm_adj
        self.layers = [64, 64, 64]

        # adverisal parameter
        self.lambda_ = 0.01
        self.eta_ = 100
        self.kappa_ = 1
        self.adv_loss = nn.MarginRankingLoss(margin=self.kappa_)
        """
        *********************************************************
        Init the weight of user-item.
        """
        self.embedding_dict, self.weight_dict = self.init_weight()
        """
        *********************************************************
        Get sparse adj.
        """
        self.sparse_norm_adj = self._convert_sp_mat_to_sp_tensor(
            self.norm_adj).to(self.device)

    def init_weight(self):
        # xavier init
        initializer = nn.init.xavier_uniform_

        embedding_dict = nn.ParameterDict({
            'user_emb':
            nn.Parameter(initializer(torch.empty(self.n_user, self.emb_size))),
            'item_emb':
            nn.Parameter(initializer(torch.empty(self.n_item, self.emb_size)))
        })

        weight_dict = nn.ParameterDict()
        layers = [self.emb_size] + self.layers
        for k in range(len(self.layers)):
            weight_dict.update({
                'W_gc_%d' % k:
                nn.Parameter(initializer(torch.empty(layers[k],
                                                     layers[k + 1])))
            })
            weight_dict.update({
                'b_gc_%d' % k:
                nn.Parameter(initializer(torch.empty(1, layers[k + 1])))
            })

            weight_dict.update({
                'W_bi_%d' % k:
                nn.Parameter(initializer(torch.empty(layers[k],
                                                     layers[k + 1])))
            })
            weight_dict.update({
                'b_bi_%d' % k:
                nn.Parameter(initializer(torch.empty(1, layers[k + 1])))
            })

        return embedding_dict, weight_dict

    def _convert_sp_mat_to_sp_tensor(self, X):
        coo = X.tocoo()
        i = torch.LongTensor([coo.row, coo.col])
        v = torch.from_numpy(coo.data).float()
        return torch.sparse.FloatTensor(i, v, coo.shape)

    def sparse_dropout(self, x, rate, noise_shape):
        random_tensor = 1 - rate
        random_tensor += torch.rand(noise_shape).to(x.device)
        dropout_mask = torch.floor(random_tensor).type(torch.bool)
        i = x._indices()
        v = x._values()

        i = i[:, dropout_mask]
        v = v[dropout_mask]

        out = torch.sparse.FloatTensor(i, v, x.shape).to(x.device)
        return out * (1. / (1 - rate))

    def create_bpr_loss(self, users, pos_items, neg_items, drop_flag=False):
        users_emb, pos_items_emb, neg_items_emb = self.get_embedding(
            users, pos_items, neg_items)
        pos_scores = torch.sum(torch.mul(users_emb, pos_items_emb), axis=1)
        neg_scores = torch.sum(torch.mul(users_emb, neg_items_emb), axis=1)

        maxi = nn.LogSigmoid()(pos_scores - neg_scores)

        bpr_loss = -1 * torch.mean(maxi)

        # cul regularizer
        regularizer = (torch.norm(users_emb)**2 + torch.norm(pos_items_emb)**2
                       + torch.norm(neg_items_emb)**2) / 2
        emb_loss = self.decay * regularizer / self.batch_size

        return bpr_loss + emb_loss, bpr_loss, emb_loss

    def rating(self, u_g_embeddings, pos_i_g_embeddings):
        return torch.matmul(u_g_embeddings, pos_i_g_embeddings.t())

    def get_embedding(self, users, pos_items, neg_items, drop_flag=False):
        """
        return users,pos_items,neg_items' embedding
        """
        A_hat = self.sparse_dropout(
            self.sparse_norm_adj, self.node_dropout,
            self.sparse_norm_adj._nnz()) if drop_flag else self.sparse_norm_adj

        ego_embeddings = torch.cat(
            [self.embedding_dict['user_emb'], self.embedding_dict['item_emb']],
            0)

        all_embeddings = [ego_embeddings]

        for k in range(len(self.layers)):
            side_embeddings = torch.sparse.mm(A_hat, ego_embeddings)

            # transformed sum messages of neighbors.
            sum_embeddings = torch.matmul(side_embeddings, self.weight_dict['W_gc_%d' % k]) \
                                             + self.weight_dict['b_gc_%d' % k]

            # bi messages of neighbors.
            # element-wise product
            bi_embeddings = torch.mul(ego_embeddings, side_embeddings)
            # transformed bi messages of neighbors.
            bi_embeddings = torch.matmul(bi_embeddings, self.weight_dict['W_bi_%d' % k]) \
                                            + self.weight_dict['b_bi_%d' % k]

            # non-linear activation.
            ego_embeddings = nn.LeakyReLU(negative_slope=0.2)(sum_embeddings +
                                                              bi_embeddings)

            # message dropout.
            if drop_flag == True:
                ego_embeddings = nn.Dropout(
                    self.mess_dropout[k])(ego_embeddings)

            # normalize the distribution of embeddings.
            norm_embeddings = F.normalize(ego_embeddings, p=2, dim=1)

            all_embeddings += [norm_embeddings]

        all_embeddings = torch.cat(all_embeddings, 1)
        u_g_embeddings = all_embeddings[:self.n_user, :]
        i_g_embeddings = all_embeddings[self.n_user:, :]
        """
        *********************************************************
        look up.
        """
        u_g_embeddings = u_g_embeddings[users, :]
        pos_i_g_embeddings = i_g_embeddings[pos_items, :]
        neg_i_g_embeddings = i_g_embeddings[neg_items, :]

        return u_g_embeddings, pos_i_g_embeddings, neg_i_g_embeddings

    def forward(self, users, pos_items, drop_flag=False):
        u_g_embeddings, pos_i_g_embeddings, _ = self.get_embedding(
            users, pos_items, [], drop_flag)

        score = torch.matmul(u_g_embeddings, pos_i_g_embeddings.t())

        return score
