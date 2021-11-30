import os

import classla
from fastapi import Body, FastAPI
from pydantic import BaseModel

from contextual_model_bert import ContextualControllerBERT
from data import Document, Token, Mention


def classla_output_to_coref_input(classla_output):
    # Transforms CLASSLA's output into a form that can be fed into coref model.
    output_tokens = {}
    output_sentences = []
    output_mentions = {}
    output_clusters = []

    current_mention_id = 1
    token_index_in_document = 0
    for sentence_index, input_sentence in enumerate(classla_output.sentences):
        output_sentence = []
        mention_tokens = []
        for token_index_in_sentence, input_token in enumerate(input_sentence.tokens):
            input_word = input_token.words[0]
            output_token = Token(str(sentence_index) + "-" + str(token_index_in_sentence),
                                 input_word.text,
                                 input_word.lemma,
                                 input_word.xpos,
                                 sentence_index,
                                 token_index_in_sentence,
                                 token_index_in_document)

            if len(mention_tokens) > 0 and mention_tokens[0].msd[0] != output_token.msd[0]:
                output_mentions[current_mention_id] = Mention(current_mention_id, mention_tokens)
                output_clusters.append([current_mention_id])
                mention_tokens = []
                current_mention_id += 1

            # TODO mention "detection"
            if output_token.msd[0] == "N" or output_token.msd[0] == "V" or output_token.msd[0] == "A":
                mention_tokens.append(output_token)

            output_tokens[output_token.token_id] = output_token
            output_sentence.append(output_token.token_id)
            token_index_in_document += 1
        output_sentences.append(output_sentence)

    return Document(1, output_tokens, output_sentences, output_mentions, output_clusters)


def init_classla():
    CLASSLA_RESOURCES_DIR = os.getenv("CLASSLA_RESOURCES_DIR", None)
    if CLASSLA_RESOURCES_DIR is None:
        raise Exception(
            "CLASSLA resources path not specified. Set environment variable CLASSLA_RESOURCES_DIR as path to the dir where CLASSLA resources should be stored.")

    processors = 'tokenize,pos,lemma,ner'
    classla.download('sl', processors=processors)
    return classla.Pipeline('sl', processors=processors)


def init_coref():
    COREF_MODEL_PATH = os.getenv("COREF_MODEL_PATH", None)
    if COREF_MODEL_PATH is None:
        raise Exception(
            "Coref model path not specified. Set environment variable COREF_MODEL_PATH as path to the model to load.")

    return ContextualControllerBERT.from_pretrained(COREF_MODEL_PATH)


classla = init_classla()
coref = init_coref()

app = FastAPI(
    title="SloCoref REST API",
    description=""
)


class _PredictCorefRequestBody(BaseModel):
    threshold: float
    return_singletons: bool
    text: str


@app.post("/predict/coref")
async def predict(
        req_body: _PredictCorefRequestBody = Body(
            example=_PredictCorefRequestBody(
                threshold=0.6,
                return_singletons=True,
                text='Janez Novak je šel v Mercator. Tam je kupil mleko. Nato ga je spreletela misel, da bi moral iti v Hofer.'
            ),
            default=None,
            media_type='application/json'
        )
):
    # 1. process input text with CLASSLA
    classla_output = classla(req_body.text)

    # 2. re-format classla_output into coref_input (incl. mention detection)
    coref_input = classla_output_to_coref_input(classla_output)

    # 3. process prepared input with coref
    coref_output = coref.evaluate_single(coref_input)

    # 4. prepare response (mentions + coreferences)
    return {
        "mentions": [  # TODO take into account given req_body's threshold and return_singletons
            {
                "id": v.mention_id,
                "start_idx": -1,  # TODO find this somehow
                "length": -1,  # TODO find this somehow
                "ner_type": "-1",  # TODO can get it from classla_output
                "msd": v.tokens[0].msd,
                "text": " ".join([t.raw_text for t in v.tokens])
            } for (k, v) in coref_input.mentions.items()],
        "coreferences": [
            sorted(
                [{
                    "mid": mention_id,
                    # TODO API suggestion is to have (mention1, mention2) pairs instead of (mention, cluster)
                    "cid": cluster_id,
                    "score": coref_output["scores"][mention_id]
                } for (mention_id, cluster_id) in coref_output["clusters"].items()],
                key=lambda x: x["mid"]
            )
        ]
    }
