from __future__ import division

import sys
import time

import numpy as np

from memn2n.memory import MemoryL, MemoryBoW
from memn2n.nn import AddTable, CrossEntropyLoss, Duplicate, ElemMult, LinearNB
from memn2n.nn import Identity, ReLU, Sequential, LookupTable, Sum, Parallel, Softmax

NSTORIES = 24000  # original: 3500
NWORDS = 50  # maximum number of words in sentence original: 20
NSENTENCES = 500  # 70000  # original: 500


def _process_input(self, data_raw):
    '''
    from https://github.com/YerevaNN/Dynamic-memory-networks-in-Theano
    '''
    questions = []
    inputs = []
    answers = []
    input_masks = []
    for x in data_raw:
        inp = x["C"].lower().split(' ') 
        inp = [w for w in inp if len(w) > 0]
        q = x["Q"].lower().split(' ')
        q = [w for w in q if len(w) > 0]
        
        inp_vector = [utils.process_word(word = w, 
                                    word2vec = self.word2vec, 
                                    vocab = self.vocab, 
                                    ivocab = self.ivocab, 
                                    word_vector_size = self.word_vector_size, 
                                    to_return = "word2vec") for w in inp]
                                    
        q_vector = [utils.process_word(word = w, 
                                    word2vec = self.word2vec, 
                                    vocab = self.vocab, 
                                    ivocab = self.ivocab, 
                                    word_vector_size = self.word_vector_size, 
                                    to_return = "word2vec") for w in q]
        
        inputs.append(np.vstack(inp_vector).astype(floatX))
        questions.append(np.vstack(q_vector).astype(floatX))
        answers.append(utils.process_word(word = x["A"], 
                                        word2vec = self.word2vec, 
                                        vocab = self.vocab, 
                                        ivocab = self.ivocab, 
                                        word_vector_size = self.word_vector_size, 
                                        to_return = "index"))
        # NOTE: here we assume the answer is one word! 
        if self.input_mask_mode == 'word':
            input_masks.append(np.array([index for index, w in enumerate(inp)], dtype=np.int32)) 
        elif self.input_mask_mode == 'sentence': 
            input_masks.append(np.array([index for index, w in enumerate(inp) if w == '.'], dtype=np.int32)) 
        else:
            raise Exception("invalid input_mask_mode")
    
    return inputs, questions, answers, input_masks

def parse_babi_task(data_files, dictionary, include_question):
    """ Parse bAbI data.

    Args:
       data_files (list): a list of data file's paths.
       dictionary (dict): word's dictionary
       include_question (bool): whether count question toward input sentence.

    Returns:
        A tuple of (story, questions, qstory):
            story (3-D array)
                [position of word in sentence, sentence index, story index] = index of word in dictionary
            questions (2-D array)
                [0-9, question index], in which the first component is encoded as follows:
                    0 - story index
                    1 - index of the last sentence before the question
                    2 - index of the answer word in dictionary
                    3 to 13 - indices of supporting sentence
                    14 - line index
            qstory (2-D array) question's indices within a story
                [index of word in question, question index] = index of word in dictionary
    """
    # Try to reserve spaces beforehand (large matrices for both 1k and 10k data sets)
    story     = np.zeros((NWORDS, NSENTENCES, len(data_files) * NSTORIES), np.int16)
    questions = np.zeros((14, len(data_files) * NSTORIES), np.int16)
    qstory    = np.zeros((NWORDS, len(data_files) * NSTORIES), np.int16)

    # NOTE: question's indices are not reset when going through a new story
    story_idx, question_idx, sentence_idx, max_words, max_sentences = -1, -1, -1, 0, 0

    # Mapping line number (within a story) to sentence's index (to support the flag include_question)
    mapping = None

    for fp in data_files:
        with open(fp) as f:
            for line_idx, line in enumerate(f):
                line = line.rstrip().lower()
                words = line.split()

                # Story begins
                if words[0] == '1':
                    story_idx += 1
                    sentence_idx = -1
                    mapping = []

                # FIXME: This condition makes the code more fragile!
                if '?' not in line:
                    is_question = False
                    sentence_idx += 1
                else:
                    is_question = True
                    question_idx += 1
                    questions[0, question_idx] = story_idx
                    questions[1, question_idx] = sentence_idx
                    if include_question:
                        sentence_idx += 1

                mapping.append(sentence_idx)

                # Skip substory index
                for k in range(1, len(words)):
                    w = words[k]

                    if w.endswith('.') or w.endswith('?'):
                        w = w[:-1]
                    if w not in dictionary:
                        # add word to the dictionary
                        dictionary[w] = len(dictionary)

                    if max_words < k:
                        max_words = k
                    # print sentence_idx, story_idx
                    if not is_question:
                        # look up word in a dictionary
                        story[k - 1, sentence_idx, story_idx] = dictionary[w]
                    else:
                        qstory[k - 1, question_idx] = dictionary[w]
                        if include_question:
                            story[k - 1, sentence_idx, story_idx] = dictionary[w]

                        # NOTE: Punctuation is already removed from w
                        if words[k].endswith('?'):
                            answer = words[k + 1]
                            if answer not in dictionary:
                                # add word to the dictionary
                                dictionary[answer] = len(dictionary)

                            questions[2, question_idx] = dictionary[answer]

                            # Indices of supporting sentences
                            for h in range(k + 2, len(words)):
                                questions[1 + h - k, question_idx] = mapping[int(words[h]) - 1]

                            questions[-1, question_idx] = line_idx
                            break

                if max_sentences < sentence_idx + 1:
                    max_sentences = sentence_idx + 1

    story     = story[:max_words, :max_sentences, :(story_idx + 1)]
    questions = questions[:, :(question_idx + 1)]
    qstory    = qstory[:max_words, :(question_idx + 1)]

    return story, questions, qstory


def build_model(general_config):
    """
    Build model

    NOTE: (for default config)
    1) Model's architecture (embedding B)
        LookupTable -> ElemMult -> Sum -> [ Duplicate -> { Parallel -> Memory -> Identity } -> AddTable ] -> LinearNB -> Softmax

    2) Memory's architecture
        a) Query module (embedding A)
            Parallel -> { LookupTable + ElemMult + Sum } -> Identity -> MatVecProd -> Softmax

        b) Output module (embedding C)
            Parallel -> { LookupTable + ElemMult + Sum } -> Identity -> MatVecProd
    """
    train_config = general_config.train_config
    dictionary   = general_config.dictionary
    use_bow      = general_config.use_bow
    nhops        = general_config.nhops
    add_proj     = general_config.add_proj
    share_type   = general_config.share_type
    enable_time  = general_config.enable_time
    add_nonlin   = general_config.add_nonlin

    in_dim    = train_config["in_dim"]
    out_dim   = train_config["out_dim"]
    max_words = train_config["max_words"]
    voc_sz    = train_config["voc_sz"]

    if not use_bow:
        train_config["weight"] = np.ones((in_dim, max_words), np.float32)
        for i in range(in_dim):
            for j in range(max_words):
                train_config["weight"][i][j] = (i + 1 - (in_dim + 1) / 2) * \
                                               (j + 1 - (max_words + 1) / 2)
        train_config["weight"] = \
            1 + 4 * train_config["weight"] / (in_dim * max_words)

    memory = {}
    model = Sequential()
    model.add(LookupTable(voc_sz, in_dim))
    if not use_bow:
        if enable_time:
            model.add(ElemMult(train_config["weight"][:, :-1]))
        else:
            model.add(ElemMult(train_config["weight"]))

    model.add(Sum(dim=1))

    proj = {}
    for i in range(nhops):
        if use_bow:
            memory[i] = MemoryBoW(train_config)
        else:
            memory[i] = MemoryL(train_config)

        # Override nil_word which is initialized in "self.nil_word = train_config["voc_sz"]"
        memory[i].nil_word = dictionary['nil']
        model.add(Duplicate())
        p = Parallel()
        p.add(memory[i])

        if add_proj:
            proj[i] = LinearNB(in_dim, in_dim)
            p.add(proj[i])
        else:
            p.add(Identity())

        model.add(p)
        model.add(AddTable())
        if add_nonlin:
            model.add(ReLU())

    model.add(LinearNB(out_dim, voc_sz, True))
    model.add(Softmax())

    # Share weights
    if share_type == 1:
        # Type 1: adjacent weight tying
        memory[0].emb_query.share(model.modules[0])
        for i in range(1, nhops):
            memory[i].emb_query.share(memory[i - 1].emb_out)

        model.modules[-2].share(memory[len(memory) - 1].emb_out)

    elif share_type == 2:
        # Type 2: layer-wise weight tying
        for i in range(1, nhops):
            memory[i].emb_query.share(memory[0].emb_query)
            memory[i].emb_out.share(memory[0].emb_out)

    if add_proj:
        for i in range(1, nhops):
            proj[i].share(proj[0])

    # Cost
    loss = CrossEntropyLoss()
    loss.size_average = False
    loss.do_softmax_bprop = True
    model.modules[-1].skip_bprop = True

    return memory, model, loss


class Progress(object):
    """
    Progress bar
    """

    def __init__(self, iterable, bar_length=50):
        self.iterable = iterable
        self.bar_length = bar_length
        self.total_length = len(iterable)
        self.start_time = time.time()
        self.count = 0

    def __iter__(self):
        for obj in self.iterable:
            yield obj
            self.count += 1
            percent = self.count / self.total_length
            print_length = int(percent * self.bar_length)
            progress = "=" * print_length + " " * (self.bar_length - print_length)
            elapsed_time = time.time() - self.start_time
            print_msg = "\r|%s| %.0f%% %.1fs" % (progress, percent * 100, elapsed_time)
            sys.stdout.write(print_msg)
            if self.count == self.total_length:
                sys.stdout.write("\r" + " " * len(print_msg) + "\r")
            sys.stdout.flush()
