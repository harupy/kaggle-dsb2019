import pandas as pd

from typing import Tuple, Optional, List

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.preprocessing import LabelEncoder
from tqdm import tqdm

from .base import Feature


class Tfidf(Feature):
    def create_features(self, train_df: pd.DataFrame, test_df: pd.DataFrame):
        le = LabelEncoder()
        train_df["title"] = le.fit_transform(train_df["title"])
        test_df["title"] = le.transform(test_df["title"])

        train_df["title_event_code"] = list(
            map(lambda x, y: str(x) + "_" + str(y), train_df["title"],
                train_df["event_code"]))
        test_df["title_event_code"] = list(
            map(lambda x, y: str(x) + "_" + str(y), test_df["title"],
                test_df["event_code"]))

        dfs_train: List[pd.DataFrame] = []
        dfs_valid: List[pd.DataFrame] = []
        dfs_test: List[pd.DataFrame] = []

        for _, user_sample in tqdm(
                train_df.groupby("installation_id", sort=False),
                total=train_df["installation_id"].nunique(),
                desc="train features"):
            if "Assessment" not in user_sample["type"].unique():
                continue
            sentences, _ = create_sentence(user_sample, test=False)
            dfs_train.append(sentences)
        train = pd.concat(dfs_train, axis=0, sort=False)
        train.reset_index(drop=True, inplace=True)

        for _, user_sample in tqdm(
                test_df.groupby("installation_id", sort=False),
                total=test_df["installation_id"].nunique(),
                desc="test features"):
            sentences, valid_sentences = create_sentence(
                user_sample, test=True)
            dfs_valid.append(valid_sentences)
            dfs_test.append(sentences)
        valid = pd.concat(dfs_valid, axis=0, sort=False)
        valid.reset_index(drop=True, inplace=True)

        test = pd.concat(dfs_test, axis=0, sort=False)
        test.reset_index(drop=True, inplace=True)

        tv = TfidfVectorizer(ngram_range=(1, 2), max_features=5000)
        tfidf_train = []
        tfidf_valid = []
        tfidf_test = []

        for col in [
                "code", "title_event_code", "code_from_last_assess",
                "title_event_code_from_last_assess"
        ]:
            tv.fit(train[col])
            colname = ["tfidf" + col + c for c in tv.get_feature_names()]
            tfidf_train.append(
                pd.DataFrame(
                    tv.transform(train[col]).toarray(), columns=colname))
            tfidf_valid.append(
                pd.DataFrame(
                    tv.transform(valid[col]).toarray(), columns=colname))
            tfidf_test.append(
                pd.DataFrame(
                    tv.transform(test[col]).toarray(), columns=colname))

        self.train = pd.concat(tfidf_train, axis=1)
        self.train.reset_index(drop=True, inplace=True)

        self.valid = pd.concat(tfidf_valid, axis=1)
        self.valid.reset_index(drop=True, inplace=True)

        self.test = pd.concat(tfidf_test, axis=1)
        self.test.reset_index(drop=True, inplace=True)


def create_sentence(user_sample: pd.DataFrame, test: bool = False
                    ) -> Tuple[pd.DataFrame, Optional[pd.DataFrame]]:
    code_sentence = ""
    title_event_code_sentence = ""
    code_from_last_assess_sentence = ""
    title_event_code_from_last_assess_sentence = ""

    df = pd.DataFrame(columns=[
        "code", "title_event_code", "code_from_last_assess",
        "title_event_code_from_last_assess"
    ])
    for sess_id, sess in user_sample.groupby("game_session", sort=False):
        sess_type = sess["type"].iloc[0]
        if sess_type == "Assessment" and (test or len(sess) > 1):
            sess_title = sess["title"].iloc[0]
            attempt_code = 4110 if (
                sess_title == "Bird Measurer (Assessment)") else 4100
            all_attempts: pd.DataFrame = sess.query(
                f"event_code == {attempt_code}")
            correct_attempt = all_attempts["event_data"].str.contains(
                "true").sum()
            failed_attempt = all_attempts["event_data"].str.contains(
                "false").sum()

            features = {
                "code":
                code_sentence,
                "title_event_code":
                title_event_code_sentence,
                "code_from_last_assess":
                code_from_last_assess_sentence,
                "title_event_code_from_last_assess":
                title_event_code_from_last_assess_sentence
            }

            if len(sess) == 1:
                df = df.append(features, ignore_index=True)
                code_from_last_assess_sentence = ""
                title_event_code_from_last_assess_sentence = ""
            elif correct_attempt + failed_attempt > 0:
                df = df.append(features, ignore_index=True)
                code_from_last_assess_sentence = ""
                title_event_code_from_last_assess_sentence = ""

        code_from_last_assess_sentence += " ".join(
            sess["event_code"].map(lambda x: str(x)).values.tolist()) + " "
        code_sentence += " ".join(
            sess["event_code"].map(lambda x: str(x)).values.tolist()) + " "
        title_event_code_from_last_assess_sentence += " ".join(
            sess["title_event_code"].map(
                lambda x: str(x)).values.tolist()) + " "
        title_event_code_sentence += " ".join(sess["title_event_code"].map(
            lambda x: str(x)).values.tolist()) + " "

    if test:
        valid_df = df.iloc[:len(df) - 1, :]
        test_df = df.iloc[len(df) - 1:len(df), :]
        return test_df, valid_df
    else:
        return df, None
