import torch
import torch.nn as nn
from torch.autograd import Variable
from torch import optim
import torch.nn.functional as F
import pdb
from utils import *


from sklearn.metrics import accuracy_score

use_cuda = torch.cuda.is_available()
dtype = torch.cuda.FloatTensor if use_cuda else torch.FloatTensor
PAD_TOKEN = 0


class charGRU(nn.Module):
    def __init__(self, l_en, options):
        super(charGRU, self).__init__()
        self.options = options
        self.l_en = l_en
        self.n_embed = options['EMBEDDING_DIM']
        self.n_dim = options['HIDDEN_DIM'] if options['HIDDEN_DIM'] % 2 == 0 else options['HIDDEN_DIM'] - 1
        self.n_out = len(options['CLASSES_2_IX'])
        self.embedding = nn.Embedding(l_en.char_vocab_size() + 1, self.n_embed).type(dtype)
        # The character lstm
        self.c_gru = nn.GRU(self.n_embed, self.n_embed, bidirectional=False).type(dtype)
        # Premise and hypothesis lstm
        self.p_gru = nn.GRU(self.n_embed, self.n_dim, bidirectional=False).type(dtype)
        self.h_gru = nn.GRU(self.n_embed, self.n_dim, bidirectional=False).type(dtype)
        self.out = nn.Linear(self.n_dim, self.n_out).type(dtype)

        # Attention Parameters
        self.W_y = nn.Parameter(torch.randn(self.n_dim, self.n_dim).cuda()) if use_cuda else nn.Parameter(torch.randn(self.n_dim, self.n_dim))  # n_dim x n_dim
        self.register_parameter('W_y', self.W_y)
        self.W_h = nn.Parameter(torch.randn(self.n_dim, self.n_dim).cuda()) if use_cuda else nn.Parameter(torch.randn(self.n_dim, self.n_dim))  # n_dim x n_dim
        self.register_parameter('W_h', self.W_h)
        self.W_alpha = nn.Parameter(torch.randn(self.n_dim, 1).cuda()) if use_cuda else nn.Parameter(torch.randn(self.n_dim, 1))  # n_dim x 1
        self.register_parameter('W_alpha', self.W_alpha)
        self.W_r = nn.Parameter(torch.randn(self.n_dim, self.n_dim).cuda()) if use_cuda else nn.Parameter(torch.randn(self.n_dim, self.n_dim))  # n_dim x n_dim
        self.register_parameter('W_r', self.W_r)

        # Match GRU parameters.
        self.m_gru = nn.GRU(self.n_dim + self.n_dim, self.n_dim, bidirectional=False).type(dtype)

    def init_hidden(self, batch_size):
        hidden_p = Variable(torch.zeros(1, batch_size, self.n_dim).type(dtype))
        hidden_h = Variable(torch.zeros(1, batch_size, self.n_dim).type(dtype))
        return hidden_p, hidden_h

    def character_hidden(self, _size):
        return Variable(torch.zeros(1, _size, self.n_embed).type(dtype))

    def attn_gru_init_hidden(self, batch_size):
        r_0 = Variable(torch.zeros(batch_size, self.n_dim).type(dtype))
        return r_0

    def mask_mult(self, o_t, o_tm1, mask_t):
        '''
            o_t : batch x n
            o_tm1 : batch x n
            mask_t : batch x 1
        '''
        # return (mask_t.expand(*o_t.size()) * o_t) + ((1. - mask_t.expand(*o_t.size())) * (o_tm1))
        return (o_t * mask_t) + (o_tm1 * (1. - mask_t))

    def _gru_forward(self, gru, encoded_s, mask_s, h_0):
        '''
        inputs :
            gru : The GRU unit for which the forward pass is to be computed
            encoded_s : T x batch x n_embed
            mask_s : T x batch
            h_0 : 1 x batch x n_dim
        outputs :
            o_s : T x batch x n_dim
            h_n : 1 x batch x n_dim
        '''
        seq_len = encoded_s.size(0)
        batch_size = encoded_s.size(1)
        o_s = Variable(torch.zeros(seq_len, batch_size, self.n_dim).type(dtype))
        h_tm1 = h_0.squeeze(0)  # batch x n_dim
        o_tm1 = None

        for ix, (x_t, mask_t) in enumerate(zip(encoded_s, mask_s)):
            '''
                x_t : batch x n_embed
                mask_t : batch,
            '''
            o_t, h_t = gru(x_t.unsqueeze(0), h_tm1.unsqueeze(0))  # o_t : 1 x batch x n_dim
                                                                  # h_t : 1 x batch x n_dim
            mask_t = mask_t.unsqueeze(1)  # batch x 1
            h_t = self.mask_mult(h_t[0], h_tm1, mask_t)

            if o_tm1 is not None:
                o_t = self.mask_mult(o_t[0], o_tm1, mask_t)
            o_tm1 = o_t[0] if o_tm1 is None else o_t
            h_tm1 = h_t
            o_s[ix] = o_t

        return o_s, h_t.unsqueeze(0)

    def _attention_forward(self, Y, mask_Y, h, r_tm1=None):
        '''
        Computes the Attention Weights over Y using h (and r_tm1 if given)
        Returns an attention weighted representation of Y, and the alphas
        inputs:
            Y : T x batch x n_dim
            mask_Y : T x batch
            h : batch x n_dim
            r_tm1 : batch x n_dim
        params:
            W_y : n_dim x n_dim
            W_h : n_dim x n_dim
            W_r : n_dim x n_dim
            W_alpha : n_dim x 1
        outputs :
            r = batch x n_dim
            alpha : batch x T
        '''
        Y = Y.transpose(1, 0)  # batch x T x n_dim
        mask_Y = mask_Y.transpose(1, 0)  # batch x T

        Wy = torch.bmm(Y, self.W_y.unsqueeze(0).expand(Y.size(0), *self.W_y.size()))  # batch x T x n_dim
        Wh = torch.mm(h, self.W_h)  # batch x n_dim
        if r_tm1 is not None:
            W_r_tm1 = torch.mm(r_tm1, self.W_r)
            Wh += W_r_tm1
        M = torch.tanh(Wy + Wh.unsqueeze(1).expand(Wh.size(0), Y.size(1), Wh.size(1)))  # batch x T x n_dim
        alpha = torch.bmm(M, self.W_alpha.unsqueeze(0).expand(Y.size(0), *self.W_alpha.size())).squeeze(-1)  # batch x T
        alpha = alpha + (-1000.0 * (1. - mask_Y))  # To ensure probability mass doesn't fall on non tokens
        alpha = F.softmax(alpha)
        return torch.bmm(alpha.unsqueeze(1), Y).squeeze(1), alpha

    def _attn_gru_forward(self, o_h, mask_h, r_0, o_p, mask_p):
        '''
        inputs:
            o_h : T x batch x n_dim : The hypothesis
            mask_h : T x batch
            r_0 : batch x n_dim :
            o_p : T x batch x n_dim : The premise. Will attend on it at every step
            mask_p : T x batch : the mask for the premise
        params:
            m_gru params
        outputs:
            r : batch x n_dim : the last state of the rnn
            alpha_vec : T x batch x T the attn vec at every step
        '''
        seq_len_h = o_h.size(0)
        batch_size = o_h.size(1)
        seq_len_p = o_p.size(0)
        alpha_vec = Variable(torch.zeros(seq_len_h, batch_size, seq_len_p).type(dtype))
        r_tm1 = r_0
        for ix, (h_t, mask_t) in enumerate(zip(o_h, mask_h)):
            '''
                h_t : batch x n_dim
                mask_t : batch,
            '''
            a_t, alpha = self._attention_forward(o_p, mask_p, h_t, r_tm1)   # a_t : batch x n_dim
                                                                            # alpha : batch x T                                                                         
            alpha_vec[ix] = alpha
            m_t = torch.cat([a_t, h_t], dim=-1)
            r_t, _ = self.m_gru(m_t.unsqueeze(0), r_tm1.unsqueeze(0))

            mask_t = mask_t.unsqueeze(1)  # batch x 1
            r_t = self.mask_mult(r_t[0], r_tm1, mask_t)
            r_tm1 = r_t

        return r_t, alpha_vec

    def _process_mask(self, char_mask, batch_size, seq_len):
        '''
        inputs:
            char_mask : The character mask : (batch*T) x W
            batch_size : The batch size
            seq_len : The sequence length
        output:
            mask_ : The word level mask : batch x T
        '''
        mask_ = torch.sum(char_mask, dim=-1, keepdim=True)  # (batch*T) x 1
        mask_ = torch.ne(mask_, 0).type(dtype)  # (batch*T) x 1
        mask_ = mask_.view(batch_size, seq_len)  # batch x T
        return mask_

    def forward(self, premise, hypothesis, training=False):
        '''
        inputs:
            premise : batch x T x W
            hypothesis : batch x T x W
        outputs :
            pred : batch x num_classes
        '''
        self.train(training)
        batch_size = premise.size(0)
        sent_len_p = premise.size(1)
        sent_len_h = hypothesis.size(1)

        # Processing the character level lstms
        premise_char = premise.view(batch_size * sent_len_p, premise.size(2))  # (batch * T) x W
        hypothesis_char = hypothesis.view(batch_size * sent_len_h, hypothesis.size(2))  # (batch * T) x W

        mask_char_p = torch.ne(premise_char, 0).type(dtype)  # (batch * T) x W
        mask_char_p = mask_char_p.transpose(1, 0)  # W x (batch * T)
        mask_char_h = torch.ne(hypothesis_char, 0).type(dtype)  # (batch * T) x W
        mask_char_h = mask_char_h.transpose(1, 0)  # W x (batch * T)
        encoded_char_p = self.embedding(premise_char)  # (batch * T) x W x n_embed
        encoded_char_p = F.dropout(encoded_char_p, p=self.options['DROPOUT'], training=training)
        p_0 = self.character_hidden(encoded_char_p.size(0))  # 1 x (batch*T) x n_embed
        encoded_char_p = encoded_char_p.transpose(1, 0)  # W x (batch * T) x n_embed

        encoded_char_h = self.embedding(hypothesis_char)  # (batch * T) x W x n_embed
        encoded_char_h = F.dropout(encoded_char_h, p=self.options['DROPOUT'], training=training)
        h_0 = self.character_hidden(encoded_char_h.size(0))  # 1 x (batch*T) x n_embed
        encoded_char_h = encoded_char_h.transpose(1, 0)  # W x (batch * T) x n_embed

        sent_p, _ = self._gru_forward(self.c_gru, encoded_char_p, mask_char_p, p_0)  # W x (batch*T) x n_embed
        sent_h, _ = self._gru_forward(self.c_gru, encoded_char_h, mask_char_h, h_0)  # W x (batch*T) x n_embed

        sent_p = sent_p[-1].view(batch_size, sent_len_p, -1)  # batch x T x n_embed
        sent_h = sent_h[-1].view(batch_size, sent_len_h, -1)  # batch x T x n_embed

        encoded_p = sent_p.transpose(1, 0)  # T x batch x n_embed
        encoded_h = sent_h.transpose(1, 0)  # T x batch x n_embed
        # Processing the masks
        mask_p = self._process_mask(mask_char_p.transpose(1, 0), batch_size, sent_len_p)  # batch x T
        mask_h = self._process_mask(mask_char_h.transpose(1, 0), batch_size, sent_len_h)  # batch x T

        mask_p = mask_p.transpose(1, 0)  # T x batch
        mask_h = mask_h.transpose(1, 0)  # T x batch

        h_p_0, h_n_0 = self.init_hidden(batch_size)  # 1 x batch x n_dim
        o_p, h_n = self._gru_forward(self.p_gru, encoded_p, mask_p, h_p_0)  # o_p : T x batch x n_dim
                                                                            # h_n : 1 x batch x n_dim

        o_h, h_n = self._gru_forward(self.h_gru, encoded_h, mask_h, h_n_0)  # o_h : T x batch x n_dim
                                                            # h_n : 1 x batch x n_dim

        r_0 = self.attn_gru_init_hidden(batch_size)
        h_star, alpha_vec = self._attn_gru_forward(o_h, mask_h, r_0, o_p, mask_p)

        h_star = self.out(h_star)  # batch x num_classes
        if self.options['LAST_NON_LINEAR']:
            h_star = F.leaky_relu(h_star)  # Non linear projection
        pred = F.log_softmax(h_star)
        return pred

    def _get_numpy_array_from_variable(self, variable):
        '''
        Converts a torch autograd variable to its corresponding numpy array
        '''
        if use_cuda:
            return variable.cpu().data.numpy()
        else:
            return variable.data.numpy()

    def fit_batch(self, premise_batch, hypothesis_batch, y_batch):
        if not hasattr(self, 'criterion'):
            self.criterion = nn.NLLLoss()
        if not hasattr(self, 'optimizer'):
            self.optimizer = optim.Adam(self.parameters(), lr=self.options['LR'], betas=(0.9, 0.999), eps=1e-08, weight_decay=self.options['L2'])

        self.optimizer.zero_grad()
        preds = self.__call__(premise_batch, hypothesis_batch, training=True)
        loss = self.criterion(preds, y_batch)
        loss.backward()
        self.optimizer.step()

        _, pred_labels = torch.max(preds, dim=-1, keepdim=True)
        y_true = self._get_numpy_array_from_variable(y_batch)
        y_pred = self._get_numpy_array_from_variable(pred_labels)
        acc = accuracy_score(y_true, y_pred)

        ret_loss = self._get_numpy_array_from_variable(loss)[0]
        return ret_loss, acc

    def process_batch(self, X_batch, y_batch=None):
        '''
        Inputs:
            X_batch : [(premise), (hypothesis)]
            y_batch : [label] or None (for predictions)
        '''
        def get_batch_row(l_en, sentence, max_sent_len, max_word_len):
            ret_val = []
            for ix in xrange(min(len(sentence), max_sent_len)):
                w = sentence[ix]
                _c = []
                for jx in xrange(min(len(w), max_word_len)):
                    _c.append(l_en.char_index(w[jx]))
                _c += [PAD_TOKEN for _ in xrange(max_word_len - len(w))]
                ret_val.append(_c)
            ret_val += [[PAD_TOKEN for _ in xrange(max_word_len)] for _ in xrange(max_sent_len - len(sentence))]
            return ret_val

        p, h = [list(x) for x in zip(*X_batch)]
        max_len_p = max([len(x) for x in p])
        max_len_h = max([len(x) for x in h])
        max_word_len_p = max([max([len(w) for w in x]) for x in p])
        max_word_len_h = max([max([len(w) for w in x]) for x in h])
        p_vec = []
        h_vec = []
        y_vec = []
        for ix in xrange(len(p)):
            p_ix = get_batch_row(self.l_en, p[ix], max_len_p, max_word_len_p)
            h_ix = get_batch_row(self.l_en, h[ix], max_len_h, max_word_len_h)
            p_vec.append(p_ix)
            h_vec.append(h_ix)
            if y_batch is not None:
                y_vec.append(self.options['CLASSES_2_IX'][y_batch[ix]])
        p_vec = np.array(p_vec, dtype=long)
        h_vec = np.array(h_vec, dtype=long)
        p_vec = Variable(torch.LongTensor(p_vec)).cuda() if use_cuda else Variable(torch.LongTensor(p_vec))
        h_vec = Variable(torch.LongTensor(h_vec)).cuda() if use_cuda else Variable(torch.LongTensor(h_vec))
        if y_batch is not None:
            y_vec = Variable(torch.LongTensor(y_vec).cuda(), requires_grad=False) if use_cuda else Variable(torch.LongTensor(y_vec), requires_grad=False)
            return p_vec, h_vec, y_vec
        else:
            return p_vec, h_vec

    def predict(self, X, batch_size=None, probs=False):
        batch_size = self.options['BATCH_SIZE'] if batch_size is None else batch_size
        preds = None
        pred_probs = None

        for ix in xrange(0, len(X), batch_size):
            p_batch, h_batch = self.process_batch(X[ix: ix + batch_size])

            preds_batch = self.__call__(p_batch, h_batch)
            _, preds_ix = torch.max(preds_batch, dim=-1, keepdim=True)
            preds_ix = self._get_numpy_array_from_variable(preds_ix)
            preds_batch = self._get_numpy_array_from_variable(preds_batch)
            if preds is None:
                if probs:
                    pred_probs = preds_batch
                else:
                    preds = preds_ix
            else:
                if probs:
                    pred_probs = np.concatenate([pred_probs, preds_batch], axis=0)
                else:
                    preds = np.concatenate([preds, preds_ix], axis=0)
        if probs:
            pred_probs = np.exp(pred_probs)
            return pred_probs
        else:
            return preds

    def fit(self, X_train, y_train, X_val, y_val, save_prefix=None, batch_size=None, n_epochs=20, steps_epoch=None):
        batch_size = self.options['BATCH_SIZE'] if batch_size is None else batch_size
        save_prefix = self.options['SAVE_PREFIX'] if save_prefix is None else save_prefix
        best_val_acc = None
        ix = 0
        steps_epoch = steps_epoch if steps_epoch is not None else ((len(X_train) // batch_size) if (len(X_train) % batch_size) == 0 else ((len(X_train) // batch_size) + 1))
        for epoch in xrange(n_epochs):
            print 'EPOCH (%d/%d)' % (epoch + 1, n_epochs)
            bar = Progbar(steps_epoch)
            for step in xrange(steps_epoch):
                X_batch = X_train[ix: ix + batch_size]
                y_batch = y_train[ix: ix + batch_size]
                premise_batch, hypothesis_batch, y_batch = self.process_batch(X_batch, y_batch)

                loss, acc = self.fit_batch(premise_batch, hypothesis_batch, y_batch)
                ix = ix + batch_size if ix + batch_size < len(X_train) else 0
                if step != (steps_epoch - 1):
                    bar.update(step + 1, values=[('train_loss', loss), ('train_acc', acc)])
                else:
                    y_pred = self.predict(X_val, batch_size, probs=False)
                    y_true = [self.options['CLASSES_2_IX'][w] for w in y_val]
                    val_acc = accuracy_score(y_true, y_pred)
                    bar.update(step + 1, values=[('train_loss', loss), ('train_acc', acc), ('val_acc', val_acc)])
                    if 'DEBUG' not in self.options or not self.options['DEBUG']:
                        if best_val_acc is None or val_acc == max(val_acc, best_val_acc):
                            best_val_acc = val_acc
                            model_name = '_epoch_%d_val_acc_%.4f.model' % (epoch + 1, val_acc)
                            model_name = save_prefix + model_name
                            torch.save(self.state_dict(), model_name)
