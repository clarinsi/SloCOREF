""" A coreference scorer that is a simplified version of the one described in End-to-end Neural Coreference Resolution.
    We are not performing end-to-end coreference resolution, so we only use the coreference scorer.
    We do not use character embeddings or additional features. For the word embeddings we use pretrained
    ELMo vectors. """
import os
import logging
import time

import numpy as np
import argparse
import torch
import torch.nn as nn
import torch.optim as optim

from allennlp.modules.elmo import Elmo, batch_to_ids
from data import read_corpus
from utils import extract_vocab, split_into_sets
from scorer import NeuralCoreferencePairScorer

parser = argparse.ArgumentParser()
parser.add_argument("--hidden_size", type=int, default=128)
parser.add_argument("--fc_hidden_size", type=int, default=150)
parser.add_argument("--dropout", type=float, default=0.2)
parser.add_argument("--learning_rate", type=float, default=0.001)
parser.add_argument("--num_epochs", type=int, default=10)
parser.add_argument("--dataset", type=str, default="coref149")
parser.add_argument("--random_seed", type=int, default=None)
parser.add_argument("--freeze_pretrained", action="store_true")


logger = logging.getLogger()
logger.setLevel(logging.INFO)

DEVICE = torch.device("cuda") if torch.cuda.is_available() else torch.device("cpu")
MODELS_SAVE_DIR = "contextual_model_elmo"
VISUALIZATION_GENERATE = True
if MODELS_SAVE_DIR and not os.path.exists(MODELS_SAVE_DIR):
    os.makedirs(MODELS_SAVE_DIR)
    logging.info(f"Created directory '{MODELS_SAVE_DIR}' for saving models.")


class ContextualController:
    def __init__(self, embedding_size,
                 hidden_size,
                 dropout,
                 pretrained_embs_dir,
                 # TODO: this really shouldn't be required (only needed for visualization)
                 dataset_name,
                 fc_hidden_size=150,
                 freeze_pretrained=True,
                 learning_rate=0.001,
                 name=None):
        self.name = name
        self.dataset_name = dataset_name
        if self.name is None:
            self.name = time.strftime("%Y%m%d_%H%M%S")

        self.path_model_dir = os.path.join(MODELS_SAVE_DIR, self.name)
        self.path_metadata = os.path.join(self.path_model_dir, "model_metadata.txt")
        self.path_pred_clusters = os.path.join(self.path_model_dir, "pred_clusters.txt")
        self.path_pred_scores = os.path.join(self.path_model_dir, "pred_scores.txt")
        self.path_log = os.path.join(self.path_model_dir, "log.txt")

        logging.info(f"Using device {DEVICE}")
        self.embedder = Elmo(options_file=os.path.join(pretrained_embs_dir, "options.json"),
                             weight_file=os.path.join(pretrained_embs_dir, "slovenian-elmo-weights.hdf5"),
                             dropout=(0.0 if freeze_pretrained else dropout),
                             num_output_representations=1,
                             requires_grad=(not freeze_pretrained)).to(DEVICE)

        self.context_encoder = nn.LSTM(input_size=embedding_size,hidden_size=hidden_size,
                                       batch_first=True, bidirectional=True).to(DEVICE)
        self.scorer = NeuralCoreferencePairScorer(num_features=(2 * hidden_size),
                                                  hidden_size=fc_hidden_size,
                                                  dropout=dropout).to(DEVICE)

        self.freeze_pretrained = freeze_pretrained
        if freeze_pretrained:
            self.optimizer = optim.Adam(list(self.context_encoder.parameters()) +
                                        list(self.scorer.parameters()),
                                        lr=learning_rate)
        else:
            self.optimizer = optim.Adam(list(self.embedder.parameters()) +
                                        list(self.context_encoder.parameters()) +
                                        list(self.scorer.parameters()),
                                        lr=learning_rate)

        self.loss = nn.CrossEntropyLoss()
        logging.debug(f"Initialized contextual ELMo-based model with name {self.name}.")
        self._prepare()

    def _prepare(self):
        """
        Prepares directories and files for the model. If directory for the model's name already exists, it tries to load
        an existing model. If loading the model was succesful, `self.loaded_from_file` is set to True.
        """
        self.loaded_from_file = False
        # Prepare directory for saving model for this run
        if not os.path.exists(self.path_model_dir):
            os.makedirs(self.path_model_dir)

            # All logs will also be written to a file
            open(self.path_log, "w", encoding="utf-8").close()
            logger.addHandler(logging.FileHandler(self.path_log, encoding="utf-8"))

            logging.info(f"Created directory '{self.path_model_dir}' for model files.")
        else:
            logging.info(f"Directory '{self.path_model_dir}' already exists.")
            path_to_model = os.path.join(self.path_model_dir, 'best_scorer.th')
            if os.path.isfile(path_to_model):
                self.scorer.load_state_dict(torch.load(path_to_model))
                self.loaded_from_file = True

            path_to_context_enc = os.path.join(self.path_model_dir, 'best_context_enc.th')
            if os.path.isfile(path_to_context_enc):
                self.context_encoder.load_state_dict(torch.load(path_to_context_enc))
                self.loaded_from_file = True

            path_to_embeddings = os.path.join(self.path_model_dir, 'best_elmo.th')
            if os.path.isfile(path_to_embeddings):
                self.embedder.load_state_dict(torch.load(path_to_embeddings))
                self.loaded_from_file = True

    def _train_doc(self, curr_doc, eval_mode=False):
        """ Trains/evaluates (if `eval_mode` is True) model on specific document.
            Returns predictions, loss and number of examples evaluated. """

        if len(curr_doc.mentions) == 0:
            return {}, (0.0, 0)

        self.optimizer.zero_grad()

        # Obtain pretrained embeddings for all tokens in current document, then encode their left and right context
        # using a bidirectional LSTM
        encoded_sents = batch_to_ids(curr_doc.raw_sentences()).to(DEVICE)
        emb_obj = self.embedder(encoded_sents)
        embeddings = emb_obj["elmo_representations"][0]  # [batch_size, max_seq_len, embedding_size]
        (lstm_encoded_sents, _) = self.context_encoder(embeddings)

        cluster_sets = []
        mention_to_cluster_id = {}
        for i, curr_cluster in enumerate(curr_doc.clusters):
            cluster_sets.append(set(curr_cluster))
            for mid in curr_cluster:
                mention_to_cluster_id[mid] = i

        doc_loss, n_examples = 0.0, 0
        preds = {}

        logging.debug(f"**Processing {len(curr_doc.mentions)} mentions...**")
        for idx_head, (head_id, head_mention) in enumerate(curr_doc.mentions.items(), 1):
            logging.debug(f"**#{idx_head} Mention '{head_id}': {head_mention}**")

            gt_antecedent_ids = cluster_sets[mention_to_cluster_id[head_id]]

            # Note: no features for dummy antecedent (len(`features`) is one less than `candidates`)
            candidates, encoded_candidates = [None], []
            gt_antecedents = []

            head_features = []
            for curr_token in head_mention.tokens:
                head_features.append(lstm_encoded_sents[curr_token.sentence_index, curr_token.position_in_sentence])
            head_features = torch.stack(head_features, dim=0)  # shape: [num_tokens, 2 * hidden_size]

            for idx_candidate, (cand_id, cand_mention) in enumerate(curr_doc.mentions.items(), 1):
                if cand_id != head_id and cand_id in gt_antecedent_ids:
                    gt_antecedents.append(idx_candidate)

                # Obtain scores for candidates and select best one as antecedent
                if idx_candidate == idx_head:
                    if len(encoded_candidates) > 0:
                        cand_scores = [torch.tensor([0.0], device=DEVICE)]
                        for candidate_features in encoded_candidates:
                            cand_scores.append(self.scorer(candidate_features, head_features))

                        assert len(cand_scores) == len(candidates)

                        # Concatenates the given sequence of seq tensors in the given dimension
                        cand_scores = torch.stack(cand_scores, dim=1)

                        # if no other antecedent exists for mention, then it's a first mention (GT is dummy antecedent)
                        if len(gt_antecedents) == 0:
                            gt_antecedents.append(0)

                        # Get index of max value. That index equals to mention at that place
                        curr_pred = torch.argmax(cand_scores)

                        # (average) loss over all ground truth antecedents
                        doc_loss += self.loss(torch.repeat_interleave(cand_scores, repeats=len(gt_antecedents), dim=0),
                                              torch.tensor(gt_antecedents, device=DEVICE))

                        n_examples += 1
                    else:
                        # Only one candidate antecedent = first mention
                        curr_pred = 0

                    # { antecedent: [mention(s)] } pair
                    existing_refs = preds.get(candidates[int(curr_pred)], [])
                    existing_refs.append(head_id)
                    preds[candidates[int(curr_pred)]] = existing_refs
                    break
                else:
                    # Add current mention as candidate
                    candidates.append(cand_id)
                    mention_features = []
                    for curr_token in cand_mention.tokens:
                        mention_features.append(lstm_encoded_sents[curr_token.sentence_index,
                                                                   curr_token.position_in_sentence])
                    mention_features = torch.stack(mention_features, dim=0)
                    encoded_candidates.append(mention_features)

        if not eval_mode:
            doc_loss.backward()
            self.optimizer.step()

        return preds, (float(doc_loss), n_examples)

    def train(self, epochs, train_docs, dev_docs):
        best_dev_loss = float("inf")
        logging.info("Starting training...")
        for idx_epoch in range(epochs):
            logging.info(f"\tRunning epoch {idx_epoch + 1}/{epochs}")

            # Make permutation of train documents
            shuffle_indices = torch.randperm(len(train_docs))

            logging.debug("\t\tModel training step")
            self.context_encoder.train()
            self.scorer.train()
            train_loss, train_examples = 0.0, 0
            for idx_doc in shuffle_indices:
                curr_doc = train_docs[idx_doc]

                _, (doc_loss, n_examples) = self._train_doc(curr_doc)

                train_loss += doc_loss
                train_examples += n_examples

            logging.debug("\t\tModel validation step")
            self.context_encoder.eval()
            self.scorer.eval()
            dev_loss, dev_examples = 0.0, 0
            for curr_doc in dev_docs:
                _, (doc_loss, n_examples) = self._train_doc(curr_doc, eval_mode=True)

                dev_loss += doc_loss
                dev_examples += n_examples

            mean_dev_loss = dev_loss / max(1, dev_examples)
            logging.info(f"\t\tTraining loss: {train_loss / max(1, train_examples): .4f}")
            logging.info(f"\t\tDev loss:      {mean_dev_loss: .4f}")

            if mean_dev_loss < best_dev_loss and MODELS_SAVE_DIR:
                logging.info(f"\tSaving new best model to '{self.path_model_dir}'")
                torch.save(self.context_encoder.state_dict(), os.path.join(self.path_model_dir, 'best_context_enc.th'))
                torch.save(self.scorer.state_dict(), os.path.join(self.path_model_dir, 'best_scorer.th'))
                if not self.freeze_pretrained:
                    torch.save(self.embedder.state_dict(), os.path.join(self.path_model_dir, 'best_elmo.th'))

                best_dev_loss = dev_loss / dev_examples


if __name__ == "__main__":
    args = parser.parse_args()
    documents = read_corpus(args.dataset)
    train_docs, dev_docs, test_docs = split_into_sets(documents, train_prop=0.7, dev_prop=0.15, test_prop=0.15)
    controller = ContextualController(embedding_size=1024,
                                      fc_hidden_size=args.fc_hidden_size,
                                      hidden_size=args.hidden_size,
                                      dropout=args.dropout,
                                      pretrained_embs_dir="../data/slovenian-elmo",
                                      freeze_pretrained=args.freeze_pretrained,
                                      learning_rate=args.learning_rate,
                                      dataset_name=args.dataset)

    controller.train(epochs=args.num_epochs, train_docs=train_docs, dev_docs=dev_docs)
