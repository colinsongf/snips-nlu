from __future__ import division
from __future__ import unicode_literals

from collections import defaultdict

import numpy as np
import scipy.sparse as sp
from builtins import object, range
from future.utils import iteritems
from sklearn.feature_extraction.text import TfidfTransformer, TfidfVectorizer
from sklearn.feature_selection import chi2
from snips_nlu_utils import normalize

from snips_nlu.builtin_entities import is_builtin_entity
from snips_nlu.constants import ENTITIES, UTTERANCES
from snips_nlu.constants import NGRAM
from snips_nlu.languages import get_default_sep
from snips_nlu.pipeline.configs import FeaturizerConfig
from snips_nlu.preprocessing import stem
from snips_nlu.resources import (get_stop_words, get_word_clusters,
                                 UnknownResource)
from snips_nlu.slot_filler.features_utils import get_all_ngrams
from snips_nlu.tokenization import tokenize_light

CLUSTER_USED_PER_LANGUAGES = {}


class Featurizer(object):
    def __init__(self, language, unknown_words_replacement_string,
                 config=FeaturizerConfig(),
                 tfidf_vectorizer=None, best_features=None,
                 entity_utterances_to_feature_names=None,
                 pvalue_threshold=0.4):
        self.config = config
        self.language = language
        if tfidf_vectorizer is None:
            tfidf_vectorizer = _get_tfidf_vectorizer(
                self.language, self.config.to_dict())
        self.tfidf_vectorizer = tfidf_vectorizer
        self.best_features = best_features
        self.pvalue_threshold = pvalue_threshold
        self.entity_utterances_to_feature_names = \
            entity_utterances_to_feature_names

        self.unknown_words_replacement_string = \
            unknown_words_replacement_string

    def fit(self, dataset, queries, y):
        utterances_to_features = _get_utterances_to_features_names(
            dataset, self.language)
        normalized_utterances_to_features = defaultdict(set)
        for k, v in iteritems(utterances_to_features):
            normalized_utterances_to_features[
                _normalize_stem(k, self.language)].update(v)
        if self.unknown_words_replacement_string is not None \
                and self.unknown_words_replacement_string in \
                normalized_utterances_to_features:
            normalized_utterances_to_features.pop(
                self.unknown_words_replacement_string)
        self.entity_utterances_to_feature_names = dict(
            normalized_utterances_to_features)

        if all(not "".join(tokenize_light(q, self.language)) for q in queries):
            return None
        preprocessed_queries = self.preprocess_queries(queries)
        # pylint: disable=C0103
        X_train_tfidf = self.tfidf_vectorizer.fit_transform(
            preprocessed_queries)
        # pylint: enable=C0103
        features_idx = {self.tfidf_vectorizer.vocabulary_[x]: x for x in
                        self.tfidf_vectorizer.vocabulary_}

        stop_words = get_stop_words(self.language)

        _, pval = chi2(X_train_tfidf, y)
        self.best_features = [i for i, v in enumerate(pval) if
                              v < self.pvalue_threshold]
        if not self.best_features:
            self.best_features = [idx for idx, val in enumerate(pval) if
                                  val == pval.min()]

        feature_names = {}
        for i in self.best_features:
            feature_names[i] = {'word': features_idx[i], 'pval': pval[i]}

        for feat in feature_names:
            if feature_names[feat]['word'] in stop_words:
                if feature_names[feat]['pval'] > self.pvalue_threshold / 2.0:
                    self.best_features.remove(feat)

        return self

    def transform(self, queries):
        preprocessed_queries = self.preprocess_queries(queries)
        # pylint: disable=C0103
        X_train_tfidf = self.tfidf_vectorizer.transform(preprocessed_queries)
        X = X_train_tfidf[:, self.best_features]
        # pylint: enable=C0103
        return X

    def fit_transform(self, dataset, queries, y):
        return self.fit(dataset, queries, y).transform(queries)

    def preprocess_queries(self, queries):
        preprocessed_queries = []
        for q in queries:
            processed_query = _preprocess_query(
                q, self.language, self.entity_utterances_to_feature_names)
            preprocessed_queries.append(processed_query)
        return preprocessed_queries

    def to_dict(self):
        """Returns a json-serializable dict"""
        if hasattr(self.tfidf_vectorizer, "vocabulary_"):
            # pylint: # pylint: disable=W0212
            vocab = {k: int(v) for k, v in
                     iteritems(self.tfidf_vectorizer.vocabulary_)}
            idf_diag = self.tfidf_vectorizer._tfidf._idf_diag.data.tolist()
            # pylint: enable=W0212
            entity_utterances_to_entity_names = {
                k: list(v)
                for k, v in iteritems(self.entity_utterances_to_feature_names)
            }
        else:
            vocab = None
            idf_diag = None
            entity_utterances_to_entity_names = dict()

        tfidf_vectorizer = {
            'vocab': vocab,
            'idf_diag': idf_diag
        }

        return {
            'language_code': self.language,
            'tfidf_vectorizer': tfidf_vectorizer,
            'best_features': self.best_features,
            'pvalue_threshold': self.pvalue_threshold,
            'entity_utterances_to_feature_names':
                entity_utterances_to_entity_names,
            'config': self.config.to_dict(),
            'unknown_words_replacement_string':
                self.unknown_words_replacement_string
        }

    @classmethod
    def from_dict(cls, obj_dict):
        """Creates a :class:`Featurizer` instance from a :obj:`dict`

        The dict must have been generated with :func:`~Featurizer.to_dict`
        """
        language = obj_dict['language_code']
        config = FeaturizerConfig.from_dict(obj_dict["config"])
        tfidf_vectorizer = _deserialize_tfidf_vectorizer(
            obj_dict["tfidf_vectorizer"], language, config)
        entity_utterances_to_entity_names = {
            k: set(v) for k, v in
            iteritems(obj_dict['entity_utterances_to_feature_names'])
        }
        self = cls(
            language=language,
            tfidf_vectorizer=tfidf_vectorizer,
            pvalue_threshold=obj_dict['pvalue_threshold'],
            entity_utterances_to_feature_names=
            entity_utterances_to_entity_names,
            best_features=obj_dict['best_features'],
            config=config,
            unknown_words_replacement_string=obj_dict[
                "unknown_words_replacement_string"]
        )
        return self


def _get_tfidf_vectorizer(language, extra_args=None):
    if extra_args is None:
        extra_args = dict()
    return TfidfVectorizer(tokenizer=lambda x: tokenize_light(x, language),
                           **extra_args)


def _get_tokens_clusters(tokens, language, cluster_name):
    clusters = get_word_clusters(language)[cluster_name]
    return [clusters[t] for t in tokens if t in clusters]


def _entity_name_to_feature(entity_name, language):
    return "entityfeature%s" % "".join(tokenize_light(
        entity_name, language=language))


def _normalize_stem(text, language):
    normalized_stemmed = normalize(text)
    try:
        normalized_stemmed = stem(normalized_stemmed, language)
    except UnknownResource:
        pass
    return normalized_stemmed


def _get_word_cluster_features(query_tokens, language):
    cluster_name = CLUSTER_USED_PER_LANGUAGES.get(language, False)
    if not cluster_name:
        return []
    ngrams = get_all_ngrams(query_tokens)
    cluster_features = []
    for ngram in ngrams:
        cluster = get_word_clusters(language)[cluster_name].get(
            ngram[NGRAM].lower(), None)
        if cluster is not None:
            cluster_features.append(cluster)
    return cluster_features


def _get_dataset_entities_features(normalized_stemmed_tokens,
                                   entity_utterances_to_entity_names):
    ngrams = get_all_ngrams(normalized_stemmed_tokens)
    entity_features = []
    for ngram in ngrams:
        entity_features += entity_utterances_to_entity_names.get(
            ngram[NGRAM], [])
    return entity_features


def _preprocess_query(query, language, entity_utterances_to_features_names):
    query_tokens = tokenize_light(query, language)
    word_clusters_features = _get_word_cluster_features(query_tokens, language)
    normalized_stemmed_tokens = [_normalize_stem(t, language)
                                 for t in query_tokens]
    entities_features = _get_dataset_entities_features(
        normalized_stemmed_tokens, entity_utterances_to_features_names)

    features = get_default_sep(language).join(normalized_stemmed_tokens)
    if entities_features:
        features += " " + " ".join(sorted(entities_features))
    if word_clusters_features:
        features += " " + " ".join(sorted(word_clusters_features))
    return features


def _get_utterances_to_features_names(dataset, language):
    utterances_to_features = defaultdict(set)
    for entity_name, entity_data in iteritems(dataset[ENTITIES]):
        if is_builtin_entity(entity_name):
            continue
        for u in entity_data[UTTERANCES]:
            utterances_to_features[u].add(_entity_name_to_feature(
                entity_name, language))
    return dict(utterances_to_features)


def _deserialize_tfidf_vectorizer(vectorizer_dict, language,
                                  featurizer_config):
    tfidf_vectorizer = _get_tfidf_vectorizer(language,
                                             featurizer_config.to_dict())
    tfidf_transformer = TfidfTransformer()
    vocab = vectorizer_dict["vocab"]
    if vocab is not None:  # If the vectorizer has been fitted
        tfidf_vectorizer.vocabulary_ = vocab
        idf_diag_data = np.array(vectorizer_dict["idf_diag"])
        idf_diag_shape = (len(idf_diag_data), len(idf_diag_data))
        row = list(range(idf_diag_shape[0]))
        col = list(range(idf_diag_shape[0]))
        idf_diag = sp.csr_matrix((idf_diag_data, (row, col)),
                                 shape=idf_diag_shape)
        tfidf_transformer._idf_diag = idf_diag  # pylint: disable=W0212
    tfidf_vectorizer._tfidf = tfidf_transformer  # pylint: disable=W0212
    return tfidf_vectorizer
