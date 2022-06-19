import os
import sys
import torch
import torch.nn as nn
import torch.nn.functional as F
from models.transformer.attention import MultiHeadAttention

from torch.nn.utils.rnn import pack_padded_sequence, pad_packed_sequence

class LangModule(nn.Module):
    def __init__(self, args, num_text_classes, use_lang_classifier=True, use_bidir=False, 
        emb_size=300, hidden_size=256, chunking=False):
        super().__init__() 

        self.args = args
        self.num_text_classes = num_text_classes
        self.use_lang_classifier = use_lang_classifier
        self.use_bidir = use_bidir

        self.language_encoder = nn.GRU(
            input_size=emb_size,
            hidden_size=hidden_size,
            batch_first=True,
            bidirectional=self.use_bidir
        )
        lang_size = hidden_size * 2 if self.use_bidir else hidden_size


        # --------- DVG fusion module ---------
        if self.args.match_module == 'dvg':
            self.fc = nn.Linear(256, 128)
            self.dropout = nn.Dropout(p=.1)
            self.layer_norm = nn.LayerNorm(128)
            self.mhatt = MultiHeadAttention(d_model=128, d_k=16, d_v=16, h=4, dropout=.1, identity_map_reordering=False,
                                            attention_module=None,
                                            attention_module_kwargs=None)

        # --------- End ---------        

        # language classifier
        if use_lang_classifier:
            self.lang_cls = nn.Sequential(
                nn.Linear(lang_size, num_text_classes),
                nn.Dropout()
            )


    def forward(self, data_dict):
        """
        encode the input descriptions
        """
        if self.args.use_chunking:
            word_embs = data_dict["lang_feat_list"]  # B * 32 * MAX_DES_LEN * LEN(300)
            lang_len = data_dict["lang_len_list"]
            batch_size, len_nun_max, max_des_len = word_embs.shape[:3]

            word_embs = word_embs.reshape(batch_size * len_nun_max, max_des_len, -1)
            lang_len = lang_len.reshape(batch_size * len_nun_max)

            lang_feat = pack_padded_sequence(word_embs, lang_len.cpu(), batch_first=True, enforce_sorted=False)

            out, lang_last = self.language_encoder(lang_feat)

            # --------- DVG fusion module ---------
            if self.args.match_module == 'dvg':
                padded = pad_packed_sequence(out, batch_first=True)
                cap_emb, cap_len = padded
                if self.use_bidir:
                    cap_emb = (cap_emb[:, :, :int(cap_emb.shape[2] / 2)] + cap_emb[:, :, int(cap_emb.shape[2] / 2):]) / 2

                b_s, seq_len = cap_emb.shape[:2]
                mask_queries = torch.ones((b_s, seq_len), dtype=torch.int)
                for i in range(b_s):
                    mask_queries[i, cap_len[i]:] = 0
                attention_mask = (mask_queries == 0).unsqueeze(1).unsqueeze(1).cuda()  # (b_s, 1, 1, seq_len)
                data_dict["attention_mask"] = attention_mask

                lang_fea = F.relu(self.fc(cap_emb))  # batch_size, n, hidden_size
                lang_fea = self.dropout(lang_fea)
                lang_fea = self.layer_norm(lang_fea)
                lang_fea = self.mhatt(lang_fea, lang_fea, lang_fea, attention_mask)

                data_dict["lang_fea"] = lang_fea

            # --------- End ---------

            data_dict["lang_feat"] = lang_feat
            
            lang_last = lang_last.permute(1, 0, 2).contiguous().flatten(start_dim=1)  # batch_size, hidden_size * num_dir
            data_dict["lang_emb"] = lang_last
            # classify
            if self.use_lang_classifier:
                data_dict["lang_scores"] = self.lang_cls(data_dict["lang_emb"])

        else:
            word_embs = data_dict["lang_feat"]

            lang_feat = pack_padded_sequence(word_embs, data_dict["lang_len"].cpu(), batch_first=True, enforce_sorted=False)
    
            # encode description
            _, lang_last = self.language_encoder(lang_feat)
            lang_last = lang_last.permute(1, 0, 2).contiguous().flatten(start_dim=1) # batch_size, hidden_size * num_dir

            # store the encoded language features
            data_dict["lang_emb"] = lang_last # B, hidden_size
        
            # classify
            if self.use_lang_classifier:
                data_dict["lang_scores"] = self.lang_cls(data_dict["lang_emb"])

        return data_dict

