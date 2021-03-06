import numpy as np
import pandas as pd
import torch
import torch.nn as nn
import torch.optim as optim
import torch.utils.data as torchdata

from typing import Union, Tuple, List, Optional
from pathlib import Path

from fastprogress import progress_bar
from sklearn.preprocessing import StandardScaler

from src.evaluation import (
    calc_metric, eval_with_truncated_data, OptimizedRounder)
from src.ensemble.blending import search_blending_weight, rmse
from .neural_network.model import (
    DSBRegressor, DSBClassifier, DSBBinary, DSBOvR, DSBRegressionOvR,
    DSBRegressionBinary, DSBRegressionOvRBinary
)
from .neural_network.loss import RMSELoss, RMSEBCELoss, RMSE2BCELoss
from .neural_network.dataset import DSBDataset
from .neural_network.importance import permutation_importance

# type alias
AoD = Union[np.ndarray, pd.DataFrame]
AoS = Union[np.ndarray, pd.Series]


class NNTrainer(object):
    def fit(
            self,
            x_train: pd.DataFrame,
            y_train: AoS,
            x_valid: pd.DataFrame,
            y_valid: AoS,
            config: dict) -> Tuple[nn.Module, dict]:
        model_params = config["model"]["model_params"]
        train_params = config["model"]["train_params"]
        mode = config["model"]["mode"]
        save_path = Path(config["model"]["save_path"])
        save_path.mkdir(parents=True, exist_ok=True)
        self.mode = mode

        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        n_categorical = len(self.categorical_features)
        n_non_categorical = len(x_train.columns) - n_categorical

        cat_dims = []
        for col in self.categorical_features:
            dim = x_train[col].nunique()
            cat_dims.append((dim, (dim // 2 + 1)))
        if mode == "regression":
            model = DSBRegressor(
                cat_dims=cat_dims,
                n_non_categorical=n_non_categorical,
                **model_params).to(device)
            loss_fn = RMSELoss().to(device)
            self.denominator = y_train.max()
            y_train_ = y_train / self.denominator
            y_valid_ = y_valid / self.denominator
        elif mode == "multiclass":
            model = DSBClassifier(
                cat_dims=cat_dims,
                n_non_categorical=n_non_categorical,
                **model_params).to(device)
            loss_fn = nn.CrossEntropyLoss().to(device)
            y_train_ = y_train
            y_valid_ = y_valid
        elif mode == "binary":
            model = DSBBinary(
                cat_dims=cat_dims,
                n_non_categorical=n_non_categorical,
                **model_params).to(device)
            loss_fn = nn.BCELoss().to(device)
            y_train_ = (y_train > 0).astype(float)
            y_valid_ = (y_valid > 0).astype(float)
        elif mode == "ovr":
            model = DSBOvR(
                cat_dims=cat_dims,
                n_non_categorical=n_non_categorical,
                **model_params).to(device)
            loss_fn = nn.BCELoss().to(device)
            y_train_ = np.asarray([
                (y_train == 0).astype(float),
                (y_train == 1).astype(float),
                (y_train == 2).astype(float),
                (y_train == 3).astype(float)
            ]).T.astype(np.float32)
            y_valid_ = np.asarray([
                (y_valid == 0).astype(float),
                (y_valid == 1).astype(float),
                (y_valid == 2).astype(float),
                (y_valid == 3).astype(float)
            ]).T.astype(np.float32)
        elif self.mode == "regression_ovr":
            model = DSBRegressionOvR(
                cat_dims=cat_dims,
                n_non_categorical=n_non_categorical,
                **model_params).to(device)
            loss_fn = RMSEBCELoss(weights=(0.2, 0.8)).to(device)
            y_train_ = y_train
            y_valid_ = y_valid
        elif self.mode == "regression_binary":
            model = DSBRegressionBinary(
                cat_dims=cat_dims,
                n_non_categorical=n_non_categorical,
                **model_params).to(device)
            loss_fn = RMSEBCELoss(weights=(0.8, 0.2)).to(device)
            y_train_ = y_train
            y_valid_ = y_valid
        elif self.mode == "regression_ovr_binary":
            model = DSBRegressionOvRBinary(
                cat_dims=cat_dims,
                n_non_categorical=n_non_categorical,
                **model_params).to(device)
            loss_fn = RMSE2BCELoss(weights=(0.1, 0.8, 0.1)).to(device)
            y_train_ = y_train
            y_valid_ = y_valid
        else:
            raise NotImplementedError

        train_dataset = DSBDataset(
            df=x_train,
            categorical_features=self.categorical_features,
            y=y_train_)
        train_loader = torchdata.DataLoader(
            train_dataset,
            batch_size=train_params["batch_size"],
            shuffle=True,
            num_workers=4)
        valid_dataset = DSBDataset(
            df=x_valid,
            categorical_features=self.categorical_features,
            y=y_valid_)
        valid_loader = torchdata.DataLoader(
            valid_dataset,
            batch_size=512,
            shuffle=False,
            num_workers=4)

        optimizer = optim.Adam(model.parameters(), lr=train_params["lr"])
        if train_params["scheduler"].get("name") is not None:
            scheduler_params = train_params.get("scheduler")
            name = scheduler_params["name"]
            if name == "cosine":
                scheduler = optim.lr_scheduler.CosineAnnealingLR(
                    optimizer,
                    T_max=scheduler_params["T_max"],
                    eta_min=scheduler_params["eta_min"])
            else:
                raise NotImplementedError

        best_score = -np.inf
        best_loss = np.inf
        for epoch in range(train_params["n_epochs"]):
            model.train()
            avg_loss = 0.0
            for non_cat, cat, target in progress_bar(train_loader):
                if self.mode == "regression_ovr":
                    y_pred_regr, y_pred_ovr = model(non_cat.float(), cat)
                    target_regr = target / 3.0
                    target_ovr = torch.from_numpy(
                        np.asarray([
                            (target.numpy() == 0.0),
                            (target.numpy() == 1.0),
                            (target.numpy() == 2.0),
                            (target.numpy() == 3.0)
                        ]).T.astype(np.float32)
                    )
                    loss = loss_fn(
                        y_pred_regr, target_regr, y_pred_ovr, target_ovr)
                elif self.mode == "regression_binary":
                    y_pred_regr, y_pred_bin = model(non_cat.float(), cat)
                    target_regr = target / 3.0
                    target_bin = (target > 0).float()
                    loss = loss_fn(
                        y_pred_regr, target_regr, y_pred_bin, target_bin)
                elif self.mode == "regression_ovr_binary":
                    y_pred_regr, y_pred_ovr, y_pred_bin = model(
                        non_cat.float(), cat)
                    target_regr = target / 3.0
                    target_ovr = torch.from_numpy(
                        np.asarray([
                            (target.numpy() == 0.0),
                            (target.numpy() == 1.0),
                            (target.numpy() == 2.0),
                            (target.numpy() == 3.0)
                        ]).T.astype(np.float32)
                    )
                    target_bin = (target > 0).float()
                    loss = loss_fn(
                        y_pred_regr, target_regr,
                        y_pred_ovr, target_ovr,
                        y_pred_bin, target_bin)
                else:
                    y_pred = model(non_cat.float(), cat)
                    if self.mode == "multiclass":
                        target = target.long()

                    loss = loss_fn(y_pred, target)
                optimizer.zero_grad()

                loss.backward()
                optimizer.step()
                avg_loss += loss.item() / len(train_loader)

            model.eval()
            if self.mode == "multiclass" or self.mode == "ovr":
                valid_preds = np.zeros((len(x_valid), 4))
            elif self.mode == "regression_ovr":
                valid_preds_regr = np.zeros(len(x_valid))
                valid_preds_ovr = np.zeros((len(x_valid), 4))
            elif self.mode == "regression_binary":
                valid_preds_regr = np.zeros(len(x_valid))
                valid_preds_bin = np.zeros(len(x_valid))
            elif self.mode == "regression_ovr_binary":
                valid_preds_regr = np.zeros(len(x_valid))
                valid_preds_ovr = np.zeros((len(x_valid), 4))
                valid_preds_bin = np.zeros(len(x_valid))
            else:
                valid_preds = np.zeros(len(x_valid))
            avg_val_loss = 0.0
            for i, (non_cat, cat, target) in enumerate(progress_bar(
                    valid_loader)):
                with torch.no_grad():
                    if self.mode == "regression_ovr":
                        y_pred_regr, y_pred_ovr = model(
                            non_cat.float(), cat)
                        y_pred_regr = y_pred_regr.detach()
                        y_pred_ovr = y_pred_ovr.detach()
                        target_regr = target / 3.0
                        target_ovr = torch.from_numpy(
                            np.asarray([
                                (target.numpy() == 0.0),
                                (target.numpy() == 1.0),
                                (target.numpy() == 2.0),
                                (target.numpy() == 3.0)
                            ]).T.astype(np.float32)
                        )
                        loss = loss_fn(
                            y_pred_regr, target_regr, y_pred_ovr, target_ovr)
                        avg_val_loss += loss.item() / len(valid_loader)
                        valid_preds_regr[
                            i*512:(i+1)*512
                        ] = y_pred_regr.cpu().numpy()
                        valid_preds_ovr[
                            i*512:(i+1)*512
                        ] = y_pred_ovr.cpu().numpy()
                    elif self.mode == "regression_binary":
                        y_pred_regr, y_pred_bin = model(non_cat.float(), cat)
                        y_pred_regr = y_pred_regr.detach()
                        y_pred_bin = y_pred_bin.detach()
                        target_regr = target / 3.0
                        target_bin = (target > 0).float()
                        loss = loss_fn(
                            y_pred_regr, target_regr, y_pred_bin, target_bin)
                        avg_val_loss += loss.item() / len(valid_loader)
                        valid_preds_regr[
                            i*512:(i+1)*512
                        ] = y_pred_regr.cpu().numpy()
                        valid_preds_bin[
                            i*512:(i+1)*512
                        ] = y_pred_bin.cpu().numpy()
                    elif self.mode == "regression_ovr_binary":
                        y_pred_regr, y_pred_ovr, y_pred_bin = model(
                            non_cat.float(), cat)
                        y_pred_regr = y_pred_regr.detach()
                        y_pred_ovr = y_pred_ovr.detach()
                        y_pred_bin = y_pred_bin.detach()
                        target_regr = target / 3.0
                        target_ovr = torch.from_numpy(
                            np.asarray([
                                (target.numpy() == 0.0),
                                (target.numpy() == 1.0),
                                (target.numpy() == 2.0),
                                (target.numpy() == 3.0)
                            ]).T.astype(np.float32)
                        )
                        target_bin = (target > 0).float()
                        loss = loss_fn(
                            y_pred_regr, target_regr,
                            y_pred_ovr, target_ovr,
                            y_pred_bin, target_bin)
                        avg_val_loss += loss.item() / len(valid_loader)
                        valid_preds_regr[
                            i*512:(i+1)*512
                        ] = y_pred_regr.cpu().numpy()
                        valid_preds_ovr[
                            i*512:(i+1)*512
                        ] = y_pred_ovr.cpu().numpy()
                        valid_preds_bin[
                            i*512:(i+1)*512
                        ] = y_pred_bin.cpu().numpy()
                    else:
                        y_pred = model(non_cat.float(), cat).detach()
                        if self.mode == "multiclass":
                            target = target.long()

                        loss = loss_fn(y_pred, target)
                        avg_val_loss += loss.item() / len(valid_loader)
                        valid_preds[
                            i*512:(i+1)*512
                        ] = y_pred.cpu().numpy()

            if self.mode == "regression" or self.mode == "binary":
                OptR = OptimizedRounder(n_overall=20, n_classwise=20)
                OptR.fit(valid_preds, y_valid.astype(int))
                valid_preds = OptR.predict(valid_preds)
                score = calc_metric(y_valid.astype(int), valid_preds)
            elif self.mode == "multiclass":
                valid_preds = valid_preds @ np.arange(4) / 3
                OptR = OptimizedRounder(n_overall=20, n_classwise=20)
                OptR.fit(valid_preds, y_valid.astype(int))
                valid_preds = OptR.predict(valid_preds)
                score = calc_metric(y_valid.astype(int), valid_preds)
            elif self.mode == "ovr":
                valid_preds = valid_preds / np.repeat(
                    valid_preds.sum(axis=1), 4).reshape(-1, 4)
                valid_preds = valid_preds @ np.arange(4) / 3
                OptR = OptimizedRounder(n_overall=20, n_classwise=20)
                OptR.fit(valid_preds, y_valid.astype(int))
                valid_preds = OptR.predict(valid_preds)
                score = calc_metric(y_valid.astype(int), valid_preds)
            elif self.mode == "regression_ovr":
                valid_preds_ovr = valid_preds_ovr / np.repeat(
                    valid_preds_ovr.sum(axis=1), 4).reshape(-1, 4)
                valid_preds_ovr = valid_preds_ovr @ np.arange(4) / 3
                _, best_weights = search_blending_weight(
                    [valid_preds_regr, valid_preds_ovr],
                    y_valid / 3.0,
                    n_iter=100,
                    func=rmse,
                    is_higher_better=False)
                valid_preds = best_weights[0] * valid_preds_regr + \
                    best_weights[1] * valid_preds_ovr
                OptR = OptimizedRounder(n_overall=20, n_classwise=20)
                OptR.fit(valid_preds, y_valid.astype(int))
                valid_preds = OptR.predict(valid_preds)
                score = calc_metric(y_valid.astype(int), valid_preds)
                OptR.fit(valid_preds_regr, y_valid.astype(int))
                valid_preds_regr = OptR.predict(valid_preds_regr)
                regr_score = calc_metric(y_valid.astype(int), valid_preds_regr)
                OptR.fit(valid_preds_ovr, y_valid.astype(int))
                valid_preds_ovr = OptR.predict(valid_preds_ovr)
                ovr_score = calc_metric(y_valid.astype(int), valid_preds_ovr)
                print(f"regr score: {regr_score:.4f}")
                print(f"ovr score: {ovr_score:.4f}")
            elif self.mode == "regression_binary":
                _, best_weights = search_blending_weight(
                    [valid_preds_regr, valid_preds_bin],
                    y_valid / 3.0,
                    n_iter=100,
                    func=rmse,
                    is_higher_better=False)
                valid_preds = best_weights[0] * valid_preds_regr + \
                    best_weights[1] * valid_preds_bin
                OptR = OptimizedRounder(n_overall=20, n_classwise=20)
                OptR.fit(valid_preds, y_valid.astype(int))
                valid_preds = OptR.predict(valid_preds)
                score = calc_metric(y_valid.astype(int), valid_preds)
                OptR.fit(valid_preds_regr, y_valid.astype(int))
                valid_preds_regr = OptR.predict(valid_preds_regr)
                regr_score = calc_metric(y_valid.astype(int), valid_preds_regr)
                OptR.fit(valid_preds_bin, y_valid.astype(int))
                valid_preds_bin = OptR.predict(valid_preds_bin)
                bin_score = calc_metric(y_valid.astype(int), valid_preds_bin)
                print(f"regr score: {regr_score:.4f}")
                print(f"bin score: {bin_score:.4f}")
            elif self.mode == "regression_ovr_binary":
                valid_preds_ovr = valid_preds_ovr / np.repeat(
                    valid_preds_ovr.sum(axis=1), 4).reshape(-1, 4)
                valid_preds_ovr = valid_preds_ovr @ np.arange(4) / 3
                _, best_weights = search_blending_weight(
                    [valid_preds_regr, valid_preds_ovr, valid_preds_bin],
                    y_valid / 3.0,
                    n_iter=100,
                    func=rmse,
                    is_higher_better=False)
                valid_preds = best_weights[0] * valid_preds_regr + \
                    best_weights[1] * valid_preds_ovr + \
                    best_weights[2] * valid_preds_bin

                OptR = OptimizedRounder(n_overall=20, n_classwise=20)
                OptR.fit(valid_preds, y_valid.astype(int))
                valid_preds = OptR.predict(valid_preds)
                score = calc_metric(y_valid.astype(int), valid_preds)

                OptR.fit(valid_preds_regr, y_valid.astype(int))
                valid_preds_regr = OptR.predict(valid_preds_regr)
                regr_score = calc_metric(y_valid.astype(int), valid_preds_regr)

                OptR.fit(valid_preds_ovr, y_valid.astype(int))
                valid_preds_ovr = OptR.predict(valid_preds_ovr)
                ovr_score = calc_metric(y_valid.astype(int), valid_preds_ovr)

                OptR.fit(valid_preds_bin, y_valid.astype(int))
                valid_preds_bin = OptR.predict(valid_preds_bin)
                bin_score = calc_metric(y_valid.astype(int), valid_preds_bin)
                print(f"regr score: {regr_score:.4f}")
                print(f"ovr score: {ovr_score:.4f}")
                print(f"bin score: {bin_score:.4f}")

            print(
                "epoch: {} loss: {:.4f} val_loss: {:.4f} qwk: {:.4f}".format(
                    epoch, avg_loss, avg_val_loss, score
                ))
            if score > best_score and avg_val_loss < best_loss:
                torch.save(
                    model.state_dict(),
                    save_path / f"best_weight_fold_{self.fold}.pth")
            if score > best_score:
                torch.save(
                    model.state_dict(),
                    save_path / f"best_score_fold_{self.fold}.pth")
                print("Achieved best score")
                best_score = score
            if avg_val_loss < best_loss:
                torch.save(
                    model.state_dict(),
                    save_path / f"best_loss_fold_{self.fold}.pth")
                print("Achieved best loss")
                best_loss = avg_val_loss

            if train_params["scheduler"].get("name") is not None:
                scheduler.step()

        if config["model"]["policy"] == "best_loss":
            weight = save_path / f"best_loss_fold_{self.fold}.pth"
        else:
            weight = save_path / f"best_score_fold_{self.fold}.pth"
        model.load_state_dict(torch.load(weight))

        model.eval()
        if self.mode == "multiclass" or self.mode == "ovr":
            valid_preds = np.zeros((len(x_valid), 4))
        elif self.mode == "regression_ovr":
            valid_preds_regr = np.zeros(len(x_valid))
            valid_preds_ovr = np.zeros((len(x_valid), 4))
        elif self.mode == "regression_binary":
            valid_preds_regr = np.zeros(len(x_valid))
            valid_preds_bin = np.zeros(len(x_valid))
        elif self.mode == "regression_ovr_binary":
            valid_preds_regr = np.zeros(len(x_valid))
            valid_preds_ovr = np.zeros((len(x_valid), 4))
            valid_preds_bin = np.zeros(len(x_valid))
        else:
            valid_preds = np.zeros(len(x_valid))
        avg_val_loss = 0.0
        for i, (non_cat, cat, target) in enumerate(
                progress_bar(valid_loader)):
            with torch.no_grad():
                if self.mode == "regression_ovr":
                    y_pred_regr, y_pred_ovr = model(
                        non_cat.float(), cat)
                    y_pred_regr = y_pred_regr.detach()
                    y_pred_ovr = y_pred_ovr.detach()
                    target_regr = target / 3.0
                    target_ovr = torch.from_numpy(
                        np.asarray([
                            (target.numpy() == 0.0),
                            (target.numpy() == 1.0),
                            (target.numpy() == 2.0),
                            (target.numpy() == 3.0)
                        ]).T.astype(np.float32)
                    )
                    loss = loss_fn(
                        y_pred_regr, target_regr, y_pred_ovr, target_ovr)
                    avg_val_loss += loss.item() / len(valid_loader)
                    valid_preds_regr[
                        i*512:(i+1)*512
                    ] = y_pred_regr.cpu().numpy()
                    valid_preds_ovr[
                        i*512:(i+1)*512
                    ] = y_pred_ovr.cpu().numpy()
                elif self.mode == "regression_binary":
                    y_pred_regr, y_pred_bin = model(non_cat.float(), cat)
                    y_pred_regr = y_pred_regr.detach()
                    y_pred_bin = y_pred_bin.detach()
                    target_regr = target / 3.0
                    target_bin = (target > 0).float()
                    loss = loss_fn(
                        y_pred_regr, target_regr, y_pred_bin, target_bin)
                    avg_val_loss += loss.item() / len(valid_loader)
                    valid_preds_regr[
                        i*512:(i+1)*512
                    ] = y_pred_regr.cpu().numpy()
                    valid_preds_bin[
                        i*512:(i+1)*512
                    ] = y_pred_bin.cpu().numpy()
                elif self.mode == "regression_ovr_binary":
                    y_pred_regr, y_pred_ovr, y_pred_bin = model(
                        non_cat.float(), cat)
                    y_pred_regr = y_pred_regr.detach()
                    y_pred_ovr = y_pred_ovr.detach()
                    y_pred_bin = y_pred_bin.detach()
                    target_regr = target / 3.0
                    target_ovr = torch.from_numpy(
                        np.asarray([
                            (target.numpy() == 0.0),
                            (target.numpy() == 1.0),
                            (target.numpy() == 2.0),
                            (target.numpy() == 3.0)
                        ]).T.astype(np.float32)
                    )
                    target_bin = (target > 0).float()
                    loss = loss_fn(
                        y_pred_regr, target_regr,
                        y_pred_ovr, target_ovr,
                        y_pred_bin, target_bin)
                    avg_val_loss += loss.item() / len(valid_loader)
                    valid_preds_regr[
                        i*512:(i+1)*512
                    ] = y_pred_regr.cpu().numpy()
                    valid_preds_ovr[
                        i*512:(i+1)*512
                    ] = y_pred_ovr.cpu().numpy()
                    valid_preds_bin[
                        i*512:(i+1)*512
                    ] = y_pred_bin.cpu().numpy()
                else:
                    y_pred = model(non_cat.float(), cat).detach()
                    if self.mode == "multiclass":
                        target = target.long()

                    loss = loss_fn(y_pred, target)
                    avg_val_loss += loss.item() / len(valid_loader)
                    valid_preds[
                        i*512:(i+1)*512
                    ] = y_pred.cpu().numpy()

        if self.mode == "regression" or self.mode == "binary":
            OptR = OptimizedRounder(n_overall=20, n_classwise=20)
            OptR.fit(valid_preds, y_valid.astype(int))
            valid_preds = OptR.predict(valid_preds)
            score = calc_metric(y_valid.astype(int), valid_preds)
        elif self.mode == "multiclass":
            valid_preds = valid_preds @ np.arange(4) / 3
            OptR = OptimizedRounder(n_overall=20, n_classwise=20)
            OptR.fit(valid_preds, y_valid.astype(int))
            valid_preds = OptR.predict(valid_preds)
            score = calc_metric(y_valid.astype(int), valid_preds)
        elif self.mode == "ovr":
            valid_preds = valid_preds / np.repeat(
                valid_preds.sum(axis=1), 4).reshape(-1, 4)
            valid_preds = valid_preds @ np.arange(4) / 3
            OptR = OptimizedRounder(n_overall=20, n_classwise=20)
            OptR.fit(valid_preds, y_valid.astype(int))
            valid_preds = OptR.predict(valid_preds)
            score = calc_metric(y_valid.astype(int), valid_preds)
        elif self.mode == "regression_ovr":
            valid_preds_ovr = valid_preds_ovr / np.repeat(
                valid_preds_ovr.sum(axis=1), 4).reshape(-1, 4)
            valid_preds_ovr = valid_preds_ovr @ np.arange(4) / 3
            best_score, best_weights = search_blending_weight(
                [valid_preds_regr, valid_preds_ovr],
                y_valid / 3.0,
                n_iter=1000,
                func=rmse,
                is_higher_better=False)
            valid_preds = best_weights[0] * valid_preds_regr + \
                best_weights[1] * valid_preds_ovr
            OptR = OptimizedRounder(n_overall=20, n_classwise=20)
            OptR.fit(valid_preds, y_valid.astype(int))
            valid_preds = OptR.predict(valid_preds)
            score = calc_metric(y_valid.astype(int), valid_preds)
            self.best_weights = best_weights
            OptR.fit(valid_preds_regr, y_valid.astype(int))
            valid_preds_regr = OptR.predict(valid_preds_regr)
            regr_score = calc_metric(y_valid.astype(int), valid_preds_regr)
            OptR.fit(valid_preds_ovr, y_valid.astype(int))
            valid_preds_ovr = OptR.predict(valid_preds_ovr)
            ovr_score = calc_metric(y_valid.astype(int), valid_preds_ovr)
            print(f"regr score: {regr_score:.4f}")
            print(f"ovr score: {ovr_score:.4f}")
        elif self.mode == "regression_binary":
            _, best_weights = search_blending_weight(
                [valid_preds_regr, valid_preds_bin],
                y_valid / 3.0,
                n_iter=1000,
                func=rmse,
                is_higher_better=False)
            valid_preds = best_weights[0] * valid_preds_regr + \
                best_weights[1] * valid_preds_bin
            OptR = OptimizedRounder(n_overall=20, n_classwise=20)
            OptR.fit(valid_preds, y_valid.astype(int))
            valid_preds = OptR.predict(valid_preds)
            score = calc_metric(y_valid.astype(int), valid_preds)
            self.best_weights = best_weights

            OptR.fit(valid_preds_regr, y_valid.astype(int))
            valid_preds_regr = OptR.predict(valid_preds_regr)
            regr_score = calc_metric(y_valid.astype(int), valid_preds_regr)
            OptR.fit(valid_preds_bin, y_valid.astype(int))
            valid_preds_bin = OptR.predict(valid_preds_bin)
            bin_score = calc_metric(y_valid.astype(int), valid_preds_bin)
            print(f"regr score: {regr_score:.4f}")
            print(f"bin score: {bin_score:.4f}")
        elif self.mode == "regression_ovr_binary":
            valid_preds_ovr = valid_preds_ovr / np.repeat(
                valid_preds_ovr.sum(axis=1), 4).reshape(-1, 4)
            valid_preds_ovr = valid_preds_ovr @ np.arange(4) / 3
            _, best_weights = search_blending_weight(
                [valid_preds_regr, valid_preds_ovr, valid_preds_bin],
                y_valid / 3.0,
                n_iter=1000,
                func=rmse,
                is_higher_better=False)
            valid_preds = best_weights[0] * valid_preds_regr + \
                best_weights[1] * valid_preds_ovr + \
                best_weights[2] * valid_preds_bin

            OptR = OptimizedRounder(n_overall=20, n_classwise=20)
            OptR.fit(valid_preds, y_valid.astype(int))
            valid_preds = OptR.predict(valid_preds)
            score = calc_metric(y_valid.astype(int), valid_preds)
            self.best_weights = best_weights
            OptR.fit(valid_preds_regr, y_valid.astype(int))
            valid_preds_regr = OptR.predict(valid_preds_regr)
            regr_score = calc_metric(y_valid.astype(int), valid_preds_regr)

            OptR.fit(valid_preds_ovr, y_valid.astype(int))
            valid_preds_ovr = OptR.predict(valid_preds_ovr)
            ovr_score = calc_metric(y_valid.astype(int), valid_preds_ovr)

            OptR.fit(valid_preds_bin, y_valid.astype(int))
            valid_preds_bin = OptR.predict(valid_preds_bin)
            bin_score = calc_metric(y_valid.astype(int), valid_preds_bin)
            print(f"regr score: {regr_score:.4f}")
            print(f"ovr score: {ovr_score:.4f}")
            print(f"bin score: {bin_score:.4f}")

        best_score_dict = {
            "loss": avg_val_loss,
            "qwk": score
        }
        return model, best_score_dict

    def predict(self, model: nn.Module, features: pd.DataFrame) -> np.ndarray:
        batch_size = 512
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        dataset = DSBDataset(
            df=features,
            categorical_features=self.categorical_features)
        loader = torchdata.DataLoader(
            dataset,
            batch_size=batch_size,
            shuffle=False,
            num_workers=4,
            drop_last=False)
        model.eval()
        model = model.to(device)
        if self.mode == "regression" or self.mode == "binary":
            predictions = np.zeros(len(features))
            for i, (non_cat, cat) in enumerate(loader):
                with torch.no_grad():
                    non_cat = non_cat.to(device)
                    cat = cat.to(device)
                    pred = model(non_cat.float(), cat).detach()
                    predictions[
                        i*batch_size: (i + 1)*batch_size
                    ] = pred.cpu().numpy()
            return predictions
        elif self.mode == "multiclass":
            predictions = np.zeros((len(features), 4))
            for i, (non_cat, cat) in enumerate(loader):
                with torch.no_grad():
                    non_cat = non_cat.to(device)
                    cat = cat.to(device)
                    pred = model(non_cat.float(), cat).detach()
                    predictions[
                        i * batch_size:(i+1) * batch_size, :
                    ] = pred.cpu().numpy()
            return predictions @ np.arange(4) / 3
        elif self.mode == "ovr":
            predictions = np.zeros((len(features), 4))
            for i, (non_cat, cat) in enumerate(loader):
                with torch.no_grad():
                    non_cat = non_cat.to(device)
                    cat = cat.to(device)
                    pred = model(non_cat.float(), cat).detach()
                    predictions[
                        i * batch_size:(i+1) * batch_size, :
                    ] = pred.cpu().numpy()
            predictions = predictions / np.repeat(
                predictions.sum(axis=1), 4).reshape(-1, 4)
            return predictions @ np.arange(4) / 3
        elif self.mode == "regression_ovr":
            pred_regr = np.zeros(len(features))
            pred_ovr = np.zeros((len(features), 4))
            for i, (non_cat, cat) in enumerate(loader):
                with torch.no_grad():
                    non_cat = non_cat.to(device)
                    cat = cat.to(device)
                    regr, ovr = model(non_cat.float(), cat)
                    regr = regr.detach()
                    ovr = ovr.detach()
                    pred_regr[
                        i * batch_size:(i+1) * batch_size
                    ] = regr.cpu().numpy()
                    pred_ovr[
                        i * batch_size:(i+1)*batch_size, :
                    ] = ovr.cpu().numpy()
            pred_ovr = pred_ovr / np.repeat(
                pred_ovr.sum(axis=1), 4).reshape(-1, 4)
            pred_ovr = pred_ovr @ np.arange(4) / 3
            predictions = self.best_weights[0] * pred_regr + \
                self.best_weights[1] * pred_ovr
            return predictions
        elif self.mode == "regression_binary":
            pred_regr = np.zeros(len(features))
            pred_bin = np.zeros(len(features))
            for i, (non_cat, cat) in enumerate(loader):
                with torch.no_grad():
                    non_cat = non_cat.to(device)
                    cat = cat.to(device)
                    regr, bin_ = model(non_cat.float(), cat)
                    regr = regr.detach()
                    bin_ = bin_.detach()
                    pred_regr[
                        i * batch_size:(i+1) * batch_size
                    ] = regr.cpu().numpy()
                    pred_bin[
                        i * batch_size:(i+1)*batch_size,
                    ] = bin_.cpu().numpy()
            predictions = self.best_weights[0] * pred_regr + \
                self.best_weights[1] * pred_bin
            return predictions
        elif self.mode == "regression_ovr_binary":
            pred_regr = np.zeros(len(features))
            pred_ovr = np.zeros((len(features), 4))
            pred_bin = np.zeros(len(features))
            for i, (non_cat, cat) in enumerate(loader):
                with torch.no_grad():
                    non_cat = non_cat.to(device)
                    cat = cat.to(device)
                    regr, ovr, bin_ = model(non_cat.float(), cat)
                    regr = regr.detach()
                    ovr = ovr.detach()
                    bin_ = bin_.detach()
                    pred_regr[
                        i * batch_size:(i+1) * batch_size
                    ] = regr.cpu().numpy()
                    pred_ovr[
                        i * batch_size:(i+1)*batch_size, :
                    ] = ovr.cpu().numpy()
                    pred_bin[
                        i * batch_size:(i+1)*batch_size,
                    ] = bin_.cpu().numpy()
            pred_ovr = pred_ovr / np.repeat(
                pred_ovr.sum(axis=1), 4).reshape(-1, 4)
            pred_ovr = pred_ovr @ np.arange(4) / 3
            predictions = self.best_weights[0] * pred_regr + \
                self.best_weights[1] * pred_ovr + \
                self.best_weights[2] * pred_bin
            return predictions
        else:
            raise NotImplementedError

    def preprocess(
            self, train_features: pd.DataFrame,
            test_features: pd.DataFrame,
            categorical_features: List[str]
            ) -> Tuple[pd.DataFrame, pd.DataFrame]:
        fill_rule = {
            "mean_var": 0.0,
            "var": np.max,
            "time_to": np.max,
            "timte_to": np.max,  # typo
            "action_time": np.mean,
            "Counter": 0.0,
            "nunique": 0.0,
            "accuracy": 0.0,
            ("num", "min"): 0.0,
            ("num", "median"): 0.0,
            ("num", "max"): 0.0,
            ("num", "sum"): 0.0,
            ("num", "last"): 0.0,
            "Ratio": 0.0,
            "ratio": 0.0,
            "mean": 0.0,
            "raito": 0.0,  # typo
            "No": 0.0,
            "n_": 0.0
        }

        n_train = len(train_features)
        n_test = len(test_features)
        concatenated_features = pd.concat([train_features, test_features],
                                          ignore_index=True)
        concatenated_features.reset_index(drop=True, inplace=True)

        # fillna
        for col in concatenated_features.columns:
            if concatenated_features[col].min() == -1.0:
                concatenated_features[col] = concatenated_features[
                    col].replace({-1.0: np.nan})

            if concatenated_features[col].hasnans:
                for key in fill_rule.keys():
                    if isinstance(key, str):
                        if key in col:
                            if isinstance(fill_rule[key], float):
                                concatenated_features[col].fillna(
                                    fill_rule[key], inplace=True)
                            else:
                                concatenated_features[col].fillna(
                                    fill_rule[key](concatenated_features[col]),
                                    inplace=True)
                    elif isinstance(key, tuple):
                        if isinstance(fill_rule[key], float):
                            concatenated_features[col].fillna(
                                fill_rule[key], inplace=True)
                        else:
                            concatenated_features[col].fillna(
                                fill_rule[key](concatenated_features[col]),
                                inplace=True)
        # log transformation
        for col in concatenated_features.columns:
            skewness = concatenated_features[col].skew()
            if -1 > skewness or 1 < skewness:
                concatenated_features[col] = np.log1p(
                    concatenated_features[col])

        scale_cols = [
            col for col in concatenated_features.columns
            if col not in categorical_features
        ]
        ss = StandardScaler()
        concatenated_features[scale_cols] = ss.fit_transform(
            concatenated_features[scale_cols])
        for col in categorical_features:
            count = 0
            ordering_dict = {}
            uniq = concatenated_features[col].unique()
            for v in uniq:
                ordering_dict[v] = count
                count += 1
            concatenated_features[col] = concatenated_features[col].map(
                lambda x: ordering_dict[x])

        train_features = concatenated_features.iloc[:n_train, :].reset_index(
            drop=True)
        test_features = concatenated_features.iloc[n_train:, :].reset_index(
            drop=True)

        assert len(train_features) == n_train
        assert len(test_features) == n_test

        return train_features, test_features

    def post_process(self, oof_preds: np.ndarray, test_preds: np.ndarray,
                     y: np.ndarray,
                     config: dict) -> Tuple[np.ndarray, np.ndarray]:
        if self.mode in [
                "multiclass", "regression",
                "binary", "ovr",
                "regression_ovr", "regression_binary",
                "regression_ovr_binary"]:
            params = config["post_process"]["params"]
            OptR = OptimizedRounder(**params)
            OptR.fit(oof_preds, y)
            oof_preds_ = OptR.predict(oof_preds)
            test_preds_ = OptR.predict(test_preds)
            return oof_preds_, test_preds_
        return oof_preds, test_preds

    def get_feature_importance(
            self,
            model: nn.Module,
            X: pd.DataFrame,
            y: np.ndarray) -> np.ndarray:
        return permutation_importance(
            model,
            X, y,
            self.categorical_features,
            self.mode)

    def cv(self, y_train: AoS, train_features: pd.DataFrame,
           test_features: pd.DataFrame, groups: np.ndarray,
           feature_name: List[str],
           categorical_features: List[str],
           folds_ids: List[Tuple[np.ndarray, np.ndarray]], threshold: float,
           config: dict) -> Tuple[List[nn.Module], np.ndarray, np.ndarray, np.
                                  ndarray, Optional[pd.DataFrame], dict]:
        test_preds = np.zeros(len(test_features))
        normal_oof_preds = np.zeros(len(train_features))
        oof_preds_list: List[np.ndarray] = []
        y_val_list: List[np.ndarray] = []
        idx_val_list: List[np.ndarray] = []

        importances = pd.DataFrame(index=feature_name)
        modes_to_return_importance = [
            "regression", "multiclass", "ovr", "binary"]
        cv_score_list: List[dict] = []
        models: List[nn.Module] = []

        self.categorical_features = categorical_features

        train_features, test_features = self.preprocess(
            train_features, test_features, categorical_features)
        X = train_features
        y = y_train.values if isinstance(y_train, pd.Series) \
            else y_train

        for i_fold, (trn_idx, val_idx) in enumerate(folds_ids):
            self.fold = i_fold
            print("=" * 20)
            print(f"Fold: {self.fold}")
            print("=" * 20)

            x_trn = X.loc[trn_idx, :]
            y_trn = y[trn_idx]

            val_idx = np.sort(val_idx)
            gp_val = groups[val_idx]

            new_idx: List[int] = []
            for gp in np.unique(gp_val):
                gp_idx = val_idx[gp_val == gp]
                new_idx.extend(gp_idx[:int(threshold)])

            x_val = X.loc[new_idx, :]
            y_val = y[new_idx]
            x_val_normal = X.loc[val_idx, :]

            model, best_score = self.fit(
                x_trn, y_trn, x_val, y_val, config=config)
            cv_score_list.append(best_score)
            models.append(model)

            oof_preds_list.append(self.predict(model, x_val).reshape(-1))
            y_val_list.append(y_val)
            idx_val_list.append(new_idx)
            normal_oof_preds[val_idx] = self.predict(
                model, x_val_normal).reshape(-1)
            test_preds += self.predict(
                model, test_features).reshape(-1) / len(folds_ids)

            if self.mode in modes_to_return_importance:
                importances_tmp = pd.DataFrame(
                    self.get_feature_importance(model, x_val, y_val),
                    columns=[f"importance_{i_fold+1}"],
                    index=feature_name)
                importances = importances.join(importances_tmp, how="inner")

        if self.mode in modes_to_return_importance:
            feature_importance = importances.mean(axis=1)
        else:
            feature_importance = None

        oof_preds = np.concatenate(oof_preds_list)
        y_oof = np.concatenate(y_val_list)
        idx_val = np.concatenate(idx_val_list)

        sorted_idx = np.argsort(idx_val)
        oof_preds = oof_preds[sorted_idx]
        y_oof = y_oof[sorted_idx]

        self.raw_oof_preds = oof_preds
        self.raw_test_preds = test_preds

        oof_preds, test_preds = self.post_process(
            oof_preds, test_preds, y_oof, config)

        oof_score = calc_metric(y_oof, oof_preds)
        print(f"oof score: {oof_score:.5f}")

        self.raw_normal_oof = normal_oof_preds

        OptR = OptimizedRounder()
        OptR.fit(normal_oof_preds, y_train)
        normal_oof_preds = OptR.predict(normal_oof_preds)
        normal_oof_score = calc_metric(y_train, normal_oof_preds)
        print(f"normal oof score: {normal_oof_score:.5f}")

        # truncated score
        truncated_result = eval_with_truncated_data(
            normal_oof_preds, y_train, groups, n_trials=100)
        print(f"truncated mean: {truncated_result['mean']:.5f}")
        print(f"truncated std: {truncated_result['std']:.5f}")

        evals_results = {
            "evals_result": {
                "oof_score":
                oof_score,
                "normal_oof_score":
                normal_oof_score,
                "truncated_eval_mean":
                truncated_result["mean"],
                "truncated_eval_0.95upper":
                truncated_result["0.95upper_bound"],
                "truncated_eval_0.95lower":
                truncated_result["0.95lower_bound"],
                "truncated_eval_std":
                truncated_result["std"],
                "cv_score": {
                    f"cv{i + 1}": cv_score
                    for i, cv_score in enumerate(cv_score_list)
                },
                "n_data":
                len(train_features),
                "n_features":
                len(train_features.columns)
            }
        }

        if self.mode in modes_to_return_importance:
            evals_results["feature_importance"] = \
                feature_importance.sort_values(ascending=False).to_dict()

        return (models, oof_preds, y_oof, test_preds,  feature_importance,
                evals_results)
