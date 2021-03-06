# Copyright 2015 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================

"""Multi-threaded word2vec unbatched skip-gram model.

Trains the model described in:
(Mikolov, et. al.) Efficient Estimation of Word Representations in Vector Space
ICLR 2013.
http://arxiv.org/abs/1301.3781
This model does true SGD (i.e. no minibatching). To do this efficiently, custom
ops are used to sequentially process data within a 'batch'.

The key ops used are:
* skipgram custom op that does input processing.
* neg_train custom op that efficiently calculates and applies the gradient using
  true SGD.
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import sys
import threading
import time

global_start = time.time()

import six
# noinspection PyUnresolvedReferences
from six.moves import xrange  # pylint: disable=redefined-builtin

import numpy as np
import tensorflow as tf

word2vec = tf.load_op_library(os.path.join(os.path.dirname(os.path.realpath(__file__)), 'word2vec_ops.so'))

flags = tf.app.flags


log_file = open('train_log.txt', 'a')
def output(*args):
    print(*args)
    line = ' '.join(str(s) for s in args)
    log_file.write(line)
    log_file.write('\n')
    log_file.flush()


flags.DEFINE_string("save_path", "savedata", "Directory to write the model.")
flags.DEFINE_boolean("load_data", False, "Load data from [save_path] instead of training from scratch (turns off training and makes interactive default ON unless --resume is on).")
flags.DEFINE_boolean("resume", False, "Whether we should continue training after load.")
flags.DEFINE_boolean("dry_run", False, "Don't save anything.")
flags.DEFINE_string(
    "train_data", None,
    "Training data. E.g., unzipped file http://mattmahoney.net/dc/text8.zip.")
flags.DEFINE_string(
    "eval_data", None, "Analogy questions. "
    "You can use value like 'name-%d.smth' to read question groups 'name-1.smth', 'name-2.smth' and so on. "
    "See README.md for how to get 'questions-words.txt'.")
flags.DEFINE_integer("embedding_size", 200, "The embedding dimension size.")
flags.DEFINE_integer(
    "epochs_to_train", 15,
    "Number of epochs to train. Each epoch processes the training data once "
    "completely.")
flags.DEFINE_float("learning_rate", 0.025, "Initial learning rate.")
flags.DEFINE_integer("num_neg_samples", 25,
                     "Negative samples per training example.")
flags.DEFINE_integer("batch_size", 500,
                     "Numbers of training examples each step processes "
                     "(no minibatching).")
flags.DEFINE_integer("concurrent_steps", 12,
                     "The number of concurrent training steps.")
flags.DEFINE_integer("window_size", 5,
                     "The number of words to predict to the left and right "
                     "of the target word.")
flags.DEFINE_integer("min_count", 5,
                     "The minimum number of word occurrences for it to be "
                     "included in the vocabulary.")
flags.DEFINE_float("subsample", 1e-3,
                   "Subsample threshold for word occurrence. Words that appear "
                   "with higher frequency will be randomly down-sampled. Set "
                   "to 0 to disable.")
flags.DEFINE_boolean(
    "interactive", None,
    "If true, enters an IPython interactive session to play with the trained "
    "model. E.g., try model.analogy(b'france', b'paris', b'russia') and "
    "model.nearby([b'proton', b'elephant', b'maxwell'])")

FLAGS = flags.FLAGS

ANALOGY_COUNT = 4


class Options(object):
    """Options used by our word2vec model."""

    def __init__(self):
        # Model options.

        # Embedding dimension.
        self.emb_dim = FLAGS.embedding_size

        # Training options.

        # The training text file.
        self.train_data = FLAGS.train_data

        # Number of negative samples per example.
        self.num_samples = FLAGS.num_neg_samples

        # The initial learning rate.
        self.learning_rate = FLAGS.learning_rate

        # Number of epochs to train. After these many epochs, the learning
        # rate decays linearly to zero and the training stops.
        self.epochs_to_train = FLAGS.epochs_to_train

        # Concurrent training steps.
        self.concurrent_steps = FLAGS.concurrent_steps

        # Number of examples for one training step.
        self.batch_size = FLAGS.batch_size

        # The number of words to predict to the left and right of the target word.
        self.window_size = FLAGS.window_size

        # The minimum number of word occurrences for it to be included in the
        # vocabulary.
        self.min_count = FLAGS.min_count

        # Subsampling threshold for word occurrence.
        self.subsample = FLAGS.subsample

        # Where to write out summaries.
        self.save_path = FLAGS.save_path
        if not os.path.exists(self.save_path):
            os.makedirs(self.save_path)

        # Eval options.

        # The text file for eval.
        self.eval_data = FLAGS.eval_data

        # Mode options
        self.interactive = FLAGS.interactive
        self.load_data = FLAGS.load_data
        self.resume = FLAGS.resume

        # load_data without resume turns off training and turns on intercativity
        if self.load_data and not self.resume:
            self.epochs_to_train = 0
            if self.interactive is None:
                self.interactive = True
        # resume implies load_data
        if self.resume:
            self.load_data = True

        output("""====================
embedding_size:   %(emb_dim)s
epochs_to_train:  %(epochs_to_train)s
learning_rate:    %(learning_rate)s
num_neg_samples:  %(num_samples)s
batch_size:       %(batch_size)s
window_size:      %(window_size)s
subsample:        %(subsample)s
====================""" % self.__dict__)


# noinspection PyAttributeOutsideInit
class Word2Vec(object):
    """Word2Vec model (Skipgram)."""

    def __init__(self, options, session):
        self._options = options
        self._session = session
        self._word2id = {}
        self._id2word = []
        self.build_graph()
        self.build_eval_graph()
        self.save_vocab()

    def read_analogies(self):
        """Reads through the analogy question file.

    Returns:
      questions: a [n, 4] numpy array containing the analogy question's
                 word ids.
      questions_skipped: questions skipped due to unknown words.
    """
        def read_file(path):
            questions = []
            questions_skipped = 0
            with open(path, "rb") as analogy_f:
                for line in analogy_f:
                    line = line.strip()
                    if not line or line.startswith(b":"):  # Skip comments and empty lines.
                        continue
                    words = line.lower().split(b" ")
                    ids = [self._word2id.get(w.strip()) for w in words]
                    if None in ids or len(ids) != 4:
                        questions_skipped += 1
                    else:
                        questions.append(np.array(ids))
            output('Eval analogy file: "%s", questions %4d, skipped %d' % (path, len(questions), questions_skipped))
            return np.array(questions, dtype=np.int32)

        blocks = []
        if '%d' in self._options.eval_data:
            idx = 1
            while os.path.exists(self._options.eval_data % idx):
                blocks.append(read_file(self._options.eval_data % idx))
                idx += 1
        else:
            blocks.append(read_file(self._options.eval_data))

        self._analogy_questions = blocks

    def build_graph(self):
        """Build the model graph."""
        opts = self._options

        # The training data. A text file.
        (words, counts, words_per_epoch, current_epoch, total_words_processed,
         examples, labels) = word2vec.skipgram_word2vec(filename=opts.train_data,
                                                        batch_size=opts.batch_size,
                                                        window_size=opts.window_size,
                                                        min_count=opts.min_count,
                                                        subsample=opts.subsample)
        (opts.vocab_words, opts.vocab_counts,
         opts.words_per_epoch) = self._session.run([words, counts, words_per_epoch])
        opts.vocab_size = len(opts.vocab_words)
        output("Data file:", opts.train_data)
        output("Vocab size:", opts.vocab_size - 1, "+ UNK")
        output("Words per epoch:", opts.words_per_epoch)

        self._id2word = opts.vocab_words
        for i, w in enumerate(self._id2word):
            self._word2id[w] = i

        # Declare all variables we need.
        # Input words embedding: [vocab_size, emb_dim]
        w_in = tf.Variable(
            tf.random_uniform(
                [opts.vocab_size,
                 opts.emb_dim], -0.5 / opts.emb_dim, 0.5 / opts.emb_dim),
            name="w_in")

        # Global step: scalar, i.e., shape [].
        w_out = tf.Variable(tf.zeros([opts.vocab_size, opts.emb_dim]), name="w_out")

        # Global step: []
        global_step = tf.Variable(0, name="global_step")

        # Linear learning rate decay.
        words_to_train = float(opts.words_per_epoch * opts.epochs_to_train)
        lr = opts.learning_rate * tf.maximum(
            0.0001,
            1.0 - tf.cast(total_words_processed, tf.float32) / words_to_train)

        # Training nodes.
        inc = global_step.assign_add(1)
        with tf.control_dependencies([inc]):
            train = word2vec.neg_train_word2vec(w_in,
                                                w_out,
                                                examples,
                                                labels,
                                                lr,
                                                vocab_count=opts.vocab_counts.tolist(),
                                                num_negative_samples=opts.num_samples)

        self._w_in = w_in
        self._examples = examples
        self._labels = labels
        self._lr = lr
        self._train = train
        self.global_step = global_step
        self._epoch = current_epoch
        self._words = total_words_processed

    def save_vocab(self):
        """Save the vocabulary to a file so the model can be reloaded."""
        opts = self._options
        with open(os.path.join(opts.save_path, "vocab.txt"), "w") as f:
            for i in xrange(opts.vocab_size):
                vocab_word = tf.compat.as_text(opts.vocab_words[i])
                f.write("%s %d\n" % (vocab_word,
                                     opts.vocab_counts[i]))

    def build_eval_graph(self):
        """Build the evaluation graph."""
        # Eval graph
        opts = self._options

        # Each analogy task is to predict the 4th word (d) given three
        # words: a, b, c.  E.g., a=italy, b=rome, c=france, we should
        # predict d=paris.

        # The eval feeds three vectors of word ids for a, b, c, each of
        # which is of size N, where N is the number of analogies we want to
        # evaluate in one batch.
        analogy_a = tf.placeholder(dtype=tf.int32)  # [N]
        analogy_b = tf.placeholder(dtype=tf.int32)  # [N]
        analogy_c = tf.placeholder(dtype=tf.int32)  # [N]

        # Normalized word embeddings of shape [vocab_size, emb_dim].
        nemb = tf.nn.l2_normalize(self._w_in, 1)

        # Each row of a_emb, b_emb, c_emb is a word's embedding vector.
        # They all have the shape [N, emb_dim]
        a_emb = tf.gather(nemb, analogy_a)  # a's embs
        b_emb = tf.gather(nemb, analogy_b)  # b's embs
        c_emb = tf.gather(nemb, analogy_c)  # c's embs

        # We expect that d's embedding vectors on the unit hyper-sphere is
        # near: c_emb + (b_emb - a_emb), which has the shape [N, emb_dim].
        target = c_emb + (b_emb - a_emb)

        # Compute cosine distance between each pair of target and vocab.
        # dist has shape [N, vocab_size].
        dist = tf.matmul(target, nemb, transpose_b=True)

        # For each question (row in dist), find the top ANALOGY_COUNT words.
        _, pred_idx = tf.nn.top_k(dist, ANALOGY_COUNT)

        # Nodes for computing neighbors for a given word according to
        # their cosine distance.
        nearby_word = tf.placeholder(dtype=tf.int32)  # word id
        nearby_emb = tf.gather(nemb, nearby_word)
        nearby_dist = tf.matmul(nearby_emb, nemb, transpose_b=True)
        nearby_val, nearby_idx = tf.nn.top_k(nearby_dist,
                                             min(1000, opts.vocab_size))

        # Nodes in the construct graph which are used by training and
        # evaluation to run/feed/fetch.
        self._analogy_a = analogy_a
        self._analogy_b = analogy_b
        self._analogy_c = analogy_c
        self._analogy_pred_idx = pred_idx
        self._nearby_word = nearby_word
        self._nearby_val = nearby_val
        self._nearby_idx = nearby_idx

        # Properly initialize all variables.
        tf.global_variables_initializer().run()

        self.saver = tf.train.Saver()

    def _train_thread_body(self):
        initial_epoch, = self._session.run([self._epoch])
        while True:
            _, epoch = self._session.run([self._train, self._epoch])
            if epoch != initial_epoch:
                break

    def train(self):
        """Train the model."""
        opts = self._options

        initial_epoch, initial_words = self._session.run([self._epoch, self._words])

        workers = []
        for _ in xrange(opts.concurrent_steps):
            t = threading.Thread(target=self._train_thread_body)
            t.start()
            workers.append(t)

        last_words, last_time = initial_words, time.time()
        while True:
            time.sleep(1)  # Reports our progress once a while.
            (epoch, step, words, lr) = self._session.run(
                [self._epoch, self.global_step, self._words, self._lr])
            now = time.time()
            last_words, last_time, rate = words, now, (words - last_words) / (now - last_time)
            print("Epoch %4d Step %8d: lr = %6.4f words/sec = %8.0f\r" % (epoch, step, lr, rate), end="")
            sys.stdout.flush()
            if epoch != initial_epoch:
                break

        for t in workers:
            t.join()

    def _predict(self, analogy):
        """Predict the top ANALOGY_COUNT answers for analogy questions."""
        idx, = self._session.run([self._analogy_pred_idx], {
            self._analogy_a: analogy[:, 0],
            self._analogy_b: analogy[:, 1],
            self._analogy_c: analogy[:, 2]
        })
        return idx

    def eval(self):
        """Evaluate analogy questions and reports accuracy."""

        print()
        multi_question = len(self._analogy_questions) > 1
        global_guessed = 0
        global_total = 0
        for i in range(len(self._analogy_questions)):
            questions = self._analogy_questions[i]
            # How many questions we get right at precision@1.
            correct = {i: 0 for i in xrange(ANALOGY_COUNT)}
            skips_map = {i: 0 for i in xrange(ANALOGY_COUNT + 1)}

            try:
                total = questions.shape[0]
            except AttributeError as e:
                raise AttributeError("Need to read analogy questions.")

            start = 0
            while start < total:
                limit = start + 2500
                sub = questions[start:limit, :]
                idx = self._predict(sub)
                start = limit
                for question in xrange(sub.shape[0]):
                    prio = 0
                    skips = 0
                    for j in xrange(ANALOGY_COUNT):
                        if idx[question, j] == sub[question, 3]:
                            # Bingo! We predicted correctly. E.g., [italy, rome, france, paris].
                            correct[prio] += 1
                            break
                        elif idx[question, j] in sub[question, :3]:
                            # We need to skip words already in the question.
                            skips += 1
                            continue
                        else:
                            # The correct label is not the precision@1
                            prio += 1
                    skips_map[skips] += 1
            accuracy_list = ' '.join('%5.1f%%' % (correct[i] * 100.0 / total) for i in xrange(ANALOGY_COUNT))
            total_skips = sum(skips_map.values())
            skips_list = ' '.join('%5.1f%%' % (skips_map[i] * 100.0 / total_skips) for i in xrange(1, ANALOGY_COUNT + 1))
            guessed = sum(correct.values())
            suffix = ' for #%d' % (i + 1) if multi_question else ''
            output("Eval%s %4d/%d accuracy = %5.1f%% [%s] skips [%s]" % (
                suffix, guessed, total, guessed * 100.0 / total, accuracy_list, skips_list
            ))
            global_guessed += guessed
            global_total += total

        if multi_question:
            output("Eval global %4d/%d accuracy = %4.1f%%" % (
                global_guessed, global_total, global_guessed * 100.0 / global_total
            ))

    def analogy(self, w0, w1, w2):
        """Predict word w3 as in w0:w1 vs w2:w3."""
        w0, w1, w2 = u2b([w0, w1, w2])
        wid = np.array([[self._word2id.get(w, 0) for w in [w0, w1, w2]]])
        idx = self._predict(wid)
        id_list = idx[0, :]
        words = [self._id2word[i] for i in id_list]
        for c in words:
            # if c not in [w0, w1, w2]:
                print(b2u(c))
        return id_list, [b2u(w) for w in words]

    def nearby(self, words, num=20):
        """Prints out nearby words given a list of words."""
        words = u2b(words)
        ids = np.array([self._word2id.get(x, 0) for x in words])
        vals, idx = self._session.run(
            [self._nearby_val, self._nearby_idx], {self._nearby_word: ids})
        for i in xrange(len(words)):
            print("\n%s\n=====================================" % (b2u(words[i])))
            for (neighbor, distance) in zip(idx[i, :num], vals[i, :num]):
                print("%-20s %6.4f" % (b2u(self._id2word[neighbor]), distance))


def _start_shell(local_ns=None):
    # An interactive shell is useful for debugging/development.
    import IPython
    user_ns = {}
    if local_ns:
        user_ns.update(local_ns)
    user_ns.update(globals())
    IPython.start_ipython(argv=[], user_ns=user_ns)


if six.PY3:
    def to_utf(u):
        return bytes(u, 'utf8')
else:
    def to_utf(u):
        return u.encode('utf8')


def u2b(s_or_l):
    if isinstance(s_or_l, six.binary_type):
        return s_or_l
    if isinstance(s_or_l, six.text_type):
        return to_utf(s_or_l)
    return [u2b(s) for s in s_or_l]


def b2u(s_or_l):
    if isinstance(s_or_l, six.text_type):
        return s_or_l
    if isinstance(s_or_l, six.binary_type):
        return six.text_type(s_or_l, 'utf8')
    return [b2u(s) for s in s_or_l]


t0 = time.time()
def print_elapsed(msg):
    global t0
    t1 = time.time()
    print("***", msg, "%.2f sec" % (t1 - t0))
    t0 = t1


def main(_):
    """Train a word2vec model."""
    if not FLAGS.train_data or not FLAGS.eval_data:
        print("--train_data --eval_data must be specified.")
        sys.exit(1)
    opts = Options()
    model_path = os.path.join(opts.save_path, "model.ckpt")
    with tf.Graph().as_default(), tf.Session() as session:
        with tf.device("/cpu:0"):
            model = Word2Vec(opts, session)
            model.read_analogies()  # Read analogy questions
        print_elapsed("model created")
        if opts.load_data:
            model.saver.restore(session, model_path)
            print_elapsed("model restored")
        model.eval()  # Eval analogies.
        for epoch in xrange(opts.epochs_to_train):
            model.train()  # Process one epoch
            model.eval()  # Eval analogies.
            print_elapsed("model train epoch %d" % epoch)
        # Perform a final save.
        if opts.epochs_to_train > 0 and not FLAGS.dry_run:
            model.saver.save(session, model_path, global_step=model.global_step)
            print_elapsed("model saved")
        if opts.interactive:
            # E.g.,
            # [0]: model.analogy(b'france', b'paris', b'russia')
            # [1]: model.nearby([b'proton', b'elephant', b'maxwell'])
            _start_shell(locals())


if __name__ == "__main__":
    import sys
    output(*(['$', sys.executable] + sys.argv))
    tf.app.run()
    log_file.close()
    print("total time: %.2f" % (time.time() - global_start))

