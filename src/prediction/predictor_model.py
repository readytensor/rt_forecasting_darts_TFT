import os
import warnings
import joblib
import numpy as np
import pandas as pd
from typing import List, Union, Optional, Tuple, Dict
from darts.models.forecasting.tft_model import TFTModel
from darts import TimeSeries
from schema.data_schema import ForecastingSchema
from sklearn.exceptions import NotFittedError
from torch import cuda
from sklearn.preprocessing import MinMaxScaler
from pytorch_lightning.callbacks.early_stopping import EarlyStopping
from logger import get_logger

warnings.filterwarnings("ignore")


PREDICTOR_FILE_NAME = "predictor.joblib"
MODEL_FILE_NAME = "model.joblib"

logger = get_logger(task_name="model")


class Forecaster:
    """A wrapper class for the TFT Forecaster.

    This class provides a consistent interface that can be used with other
    Forecaster models.
    """

    model_name = "TFT Forecaster"

    def __init__(
        self,
        data_schema: ForecastingSchema,
        input_chunk_length: int = None,
        output_chunk_length: int = None,
        history_forecast_ratio: int = None,
        lags_forecast_ratio: int = None,
        hidden_size: Union[int, List[int]] = 16,
        lstm_layers: int = 1,
        num_attention_heads: int = 4,
        full_attention: bool = False,
        feed_forward: str = "GatedResidualNetwork",
        dropout: float = 0.0,
        hidden_continuous_size: int = 8,
        categorical_embedding_sizes: Optional[
            Dict[str, Union[int, Tuple[int, int]]]
        ] = None,
        add_relative_index: bool = False,
        use_static_covariates: bool = False,
        early_stopping: bool = True,
        early_stopping_patience: int = 20,
        min_delta: float = 0.01,
        optimizer_kwargs: Optional[dict] = None,
        use_past_covariates: bool = True,
        use_future_covariates: bool = True,
        random_state: int = 0,
        **kwargs,
    ):
        """Construct a new TFT Forecaster

        Args:
            input_chunk_length (int):
                Number of time steps in the past to take as a model input (per chunk).
                Applies to the target series, and past and/or future covariates (if the model supports it).

                Note: If this parameter is not specified, lags_forecast_ratio has to be specified.

            output_chunk_length (int):
                Number of time steps predicted at once (per chunk) by the internal model.
                Also, the number of future values from future covariates to use as a model input (if the model supports future covariates).
                It is not the same as forecast horizon n used in predict(),
                which is the desired number of prediction points generated using either a one-shot- or auto-regressive forecast.
                Setting n <= output_chunk_length prevents auto-regression.
                This is useful when the covariates don't extend far enough into the future,
                or to prohibit the model from using future values of past and / or future covariates for prediction
                (depending on the model's covariate support).

                Note: If this parameter is not specified, lags_forecast_ratio has to be specified.


            history_forecast_ratio (int):
                Sets the history length depending on the forecast horizon.
                For example, if the forecast horizon is 20 and the history_forecast_ratio is 10,
                history length will be 20*10 = 200 samples.

            lags_forecast_ratio (int):
                Sets the input_chunk_length and output_chunk_length parameters depending on the forecast horizon.
                input_chunk_length = forecast horizon * lags_forecast_ratio
                output_chunk_length = forecast horizon

            hidden_size (Union[int, List[int]]):
                Hidden state size of the TFT. It is the main hyper-parameter and common across the internal TFT architecture.

            lstm_layers (int):
                Number of layers for the Long Short Term Memory (LSTM) Encoder and Decoder (1 is a good default).

            num_attention_heads (int):
                Number of attention heads (4 is a good default)

            full_attention (bool):
                If False, only attends to previous time steps in the decoder.
                If True attends to previous, current, and future time steps. Defaults to False.

            feed_forward (str):
                A feedforward network is a fully-connected layer with an activation.
                TFT Can be one of the glu variant's FeedForward Network (FFN).
                The glu variant's FeedForward Network are a series of FFNs designed to work better with Transformer based models.
                Defaults to "GatedResidualNetwork". [“GLU”, “Bilinear”, “ReGLU”, “GEGLU”, “SwiGLU”, “ReLU”, “GELU”] or the TFT original FeedForward Network [“GatedResidualNetwork”].

            dropout (float):
                The dropout probability to be used in fully connected layers.
                This is compatible with Monte Carlo dropout at inference time for model uncertainty estimation (enabled with mc_dropout=True at prediction time).

            hidden_continuous_size (int):
                Default for hidden size for processing continuous variables

            categorical_embedding_sizes (Optional[Dict[str, Union[int, Tuple[int, int]]]]):
                A dictionary used to construct embeddings for categorical static covariates.
                The keys are the column names of the categorical static covariates.
                Each value is either a single integer or a tuple of integers.
                For a single integer give the number of unique categories (n) of the corresponding variable.
                For example {"some_column": 64}. The embedding size will be automatically determined by min(round(1.6 * n**0.56), 100).
                For a tuple of integers, give (number of unique categories, embedding size). For example {"some_column": (64, 8)}.
                Note that TorchForecastingModels only support numeric data.
                Consider transforming/encoding your data with darts.dataprocessing.transformers.static_covariates_transformer.StaticCovariatesTransformer.

            add_relative_index (bool):
                Whether to add positional values to future covariates.
                Defaults to False. This allows to use the TFTModel without having to pass future_covariates to fit() and train().
                It gives a value to the position of each step from input and output chunk relative to the prediction point.
                The values are normalized with input_chunk_length.

            use_static_covariates (bool):
                Whether the model should use static covariate information in case the input series passed to fit() contain static covariates.
                If True, and static covariates are available at fitting time, will enforce that all target series have the same static covariate dimensionality in fit() and predict().

            optimizer_kwargs:
                Optionally, some keyword arguments for the PyTorch optimizer (e.g., {'lr': 1e-3} for specifying a learning rate).
                Otherwise the default values of the selected optimizer_cls will be used. Default: None.

            early_stopping (bool): If true, use early stopping.

            early_stopping_patience (int): Patience used by early stopper.

            min_delta (float): Minimum imporovement required by early stopper.

            random_state (int):
                Sets the underlying random seed at model initialization time.

            use_past_covariates (bool):
                Whether the model should use past covariates if available.

            use_future_covariates (bool):
                Whether the model should use future covariates if available.

            **kwargs:
                Optional arguments to initialize the pytorch_lightning.Module, pytorch_lightning.Trainer, and Darts' TorchForecastingModel.
        """
        self.data_schema = data_schema
        self.input_chunk_length = input_chunk_length
        self.output_chunk_length = output_chunk_length
        self.hidden_size = hidden_size
        self.lstm_layers = lstm_layers
        self.num_attention_heads = num_attention_heads
        self.full_attention = full_attention
        self.feed_forward = feed_forward
        self.dropout = dropout
        self.hidden_continuous_size = hidden_continuous_size
        self.categorical_embedding_sizes = categorical_embedding_sizes
        self.add_relative_index = add_relative_index
        self.use_static_covariates = use_static_covariates
        self.optimizer_kwargs = optimizer_kwargs

        self.use_past_covariates = (
            use_past_covariates and len(data_schema.past_covariates) > 0
        )
        self.use_future_covariates = use_future_covariates and (
            len(data_schema.future_covariates) > 0
            or self.data_schema.time_col_dtype in ["DATE", "DATETIME"]
        )
        self.use_static_covariates = (
            use_static_covariates and len(data_schema.static_covariates) > 0
        )

        self.random_state = random_state
        self.kwargs = kwargs
        self._is_trained = False
        self.history_length = None

        if history_forecast_ratio:
            self.history_length = (
                self.data_schema.forecast_length * history_forecast_ratio
            )

        if lags_forecast_ratio:
            lags = self.data_schema.forecast_length * lags_forecast_ratio
            self.input_chunk_length = lags

        if not self.output_chunk_length:
            self.output_chunk_length = self.data_schema.forecast_length

        stopper = EarlyStopping(
            monitor="train_loss",
            patience=early_stopping_patience,
            min_delta=min_delta,
            mode="min",
        )

        self.pl_trainer_kwargs = {}
        if early_stopping:
            self.pl_trainer_kwargs["callbacks"] = [stopper]

        if cuda.is_available():
            self.pl_trainer_kwargs["accelerator"] = "gpu"
            print("GPU training is available.")
        else:
            print("GPU training not available.")

    def _prepare_data(
        self,
        history: pd.DataFrame,
        data_schema: ForecastingSchema,
    ) -> Tuple[List, List, List]:
        """
        Puts the data into the expected shape by the forecaster.
        Drops the time column and puts all the target series as columns in the dataframe.

        Args:
            history (pd.DataFrame): The provided training data.
            data_schema (ForecastingSchema): The schema of the training data.

        Returns:
            Tuple[List, List, List]: Target, Past covariates and Future covariates.
        """
        targets = []
        past = []
        future = []

        future_covariates_names = data_schema.future_covariates
        if data_schema.time_col_dtype in ["DATE", "DATETIME"]:
            date_col = pd.to_datetime(history[data_schema.time_col])
            year_col = date_col.dt.year
            month_col = date_col.dt.month
            year_col_name = f"{data_schema.time_col}_year"
            month_col_name = f"{data_schema.time_col}_month"
            history[year_col_name] = year_col
            history[month_col_name] = month_col
            future_covariates_names += [year_col_name, month_col_name]

            year_col = date_col.dt.year
            month_col = date_col.dt.month

        groups_by_ids = history.groupby(data_schema.id_col)
        all_ids = list(groups_by_ids.groups.keys())
        all_series = [
            groups_by_ids.get_group(id_).drop(columns=data_schema.id_col)
            for id_ in all_ids
        ]

        self.all_ids = all_ids
        scalers = {}
        for index, s in enumerate(all_series):
            if self.history_length:
                s = s.iloc[-self.history_length :]
            s.reset_index(inplace=True)

            past_scaler = MinMaxScaler()
            scaler = MinMaxScaler()
            s[data_schema.target] = scaler.fit_transform(
                s[data_schema.target].values.reshape(-1, 1)
            )

            scalers[index] = scaler
            static_covariates = None
            if self.use_static_covariates and self.data_schema.static_covariates:
                static_covariates = s[self.data_schema.static_covariates]

            target = TimeSeries.from_dataframe(
                s,
                value_cols=data_schema.target,
                static_covariates=(
                    static_covariates.iloc[0] if static_covariates is not None else None
                ),
            )

            targets.append(target)

            if data_schema.past_covariates:
                original_values = (
                    s[data_schema.past_covariates].values.reshape(-1, 1)
                    if len(data_schema.past_covariates) == 1
                    else s[data_schema.past_covariates].values
                )
                s[data_schema.past_covariates] = past_scaler.fit_transform(
                    original_values
                )
                past_covariates = TimeSeries.from_dataframe(
                    s[data_schema.past_covariates]
                )
                past.append(past_covariates)

        future_scalers = {}
        if future_covariates_names:
            for id, train_series in zip(all_ids, all_series):
                if self.history_length:
                    train_series = train_series.iloc[-self.history_length :]

                future_covariates = train_series[future_covariates_names]

                future_covariates.reset_index(inplace=True)
                future_scaler = MinMaxScaler()
                original_values = (
                    future_covariates[future_covariates_names].values.reshape(-1, 1)
                    if len(future_covariates_names) == 1
                    else future_covariates[future_covariates_names].values
                )
                future_covariates[future_covariates_names] = (
                    future_scaler.fit_transform(original_values)
                )

                future_covariates = TimeSeries.from_dataframe(
                    future_covariates[future_covariates_names]
                )
                future_scalers[id] = future_scaler
                future.append(future_covariates)

        self.scalers = scalers
        self.future_scalers = future_scalers
        if not past or not self.use_past_covariates:
            past = None
        if not future or not self.use_future_covariates:
            future = None

        return targets, past, future

    def _prepare_test_data(
        self,
        data: pd.DataFrame,
    ) -> List:
        """
        Prepares testing data.

        Args:
            data (pd.DataFrame): Testing data.

        Returns (List): Training and testing future covariates concatenated together.

        """
        future = []
        data_schema = self.data_schema
        future_covariates_names = data_schema.future_covariates
        if data_schema.time_col_dtype in ["DATE", "DATETIME"]:
            date_col = pd.to_datetime(data[data_schema.time_col])
            year_col = date_col.dt.year
            month_col = date_col.dt.month
            year_col_name = f"{data_schema.time_col}_year"
            month_col_name = f"{data_schema.time_col}_month"
            data[year_col_name] = year_col
            data[month_col_name] = month_col
            year_col = date_col.dt.year
            month_col = date_col.dt.month

        groups_by_ids = data.groupby(data_schema.id_col)
        all_ids = list(groups_by_ids.groups.keys())
        all_series = [
            groups_by_ids.get_group(id_).drop(columns=data_schema.id_col)
            for id_ in all_ids
        ]

        if future_covariates_names:
            for id, test_series in zip(all_ids, all_series):
                future_covariates = test_series[future_covariates_names]

                future_covariates.reset_index(inplace=True)
                future_scaler = self.future_scalers[id]
                original_values = (
                    future_covariates[future_covariates_names].values.reshape(-1, 1)
                    if len(future_covariates_names) == 1
                    else future_covariates[future_covariates_names].values
                )

                future_covariates[future_covariates_names] = future_scaler.transform(
                    original_values
                )

                future_covariates = TimeSeries.from_dataframe(
                    future_covariates[future_covariates_names]
                )
                future.append(future_covariates)

        if not future or not self.use_future_covariates:
            future = None
        else:
            for index, (train_covariates, test_covariates) in enumerate(
                zip(self.training_future_covariates, future)
            ):
                train_values = train_covariates.values()
                test_values = test_covariates.values()

                full_values = np.concatenate((train_values, test_values), axis=0)
                full_series = TimeSeries.from_values(full_values)

                future[index] = full_series

        return future

    def _validate_input_chunk_and_history_lengths(self, series_length: int) -> None:
        """
        Validate the value of input_chunk_length and that history length is at least double the forecast horizon.
        If the provided input_chunk_length value is invalid (too large), input_chunk_length are set to the largest possible value.

        Args:
            series_length (int): The length of the history.

        Returns: None
        """

        if series_length < 2 * self.data_schema.forecast_length:
            raise ValueError(
                f"Training series is too short. History should be at least double the forecast horizon. history_length = ({series_length}), forecast horizon = ({self.data_schema.forecast_length})"
            )

        if self.input_chunk_length > series_length - self.output_chunk_length:
            logger.warning(
                f"histroy_length is ({series_length}) and output_chunk_length is ({self.output_chunk_length})."
                f" input_chunk_length cannot exceed the value of (histroy_length - output_chunk_length). Setting input_chunk_length = ({series_length - self.output_chunk_length})"
            )
            self.input_chunk_length = series_length - self.output_chunk_length

        elif self.input_chunk_length > series_length:
            self.input_chunk_length = series_length - self.output_chunk_length
            logger.warning(
                "The provided input_chunk_length value is greater than the available history length."
                f" input_chunk_length are set to to (history length - forecast horizon) = {self.input_chunk_length}"
            )

    def fit(
        self,
        history: pd.DataFrame,
        data_schema: ForecastingSchema,
    ) -> None:
        """Fit the Forecaster to the training data.
        A separate LinearRegression model is fit to each series that is contained
        in the data.

        Args:
            history (pandas.DataFrame): The features of the training data.
            data_schema (ForecastingSchema): The schema of the training data.

        """
        np.random.seed(self.random_state)
        targets, past_covariates, future_covariates = self._prepare_data(
            history=history,
            data_schema=data_schema,
        )

        self._validate_input_chunk_and_history_lengths(series_length=len(targets[0]))

        self.model = TFTModel(
            input_chunk_length=self.input_chunk_length,
            output_chunk_length=self.output_chunk_length,
            hidden_size=self.hidden_size,
            lstm_layers=self.lstm_layers,
            num_attention_heads=self.num_attention_heads,
            full_attention=self.full_attention,
            feed_forward=self.feed_forward,
            dropout=self.dropout,
            hidden_continuous_size=self.hidden_continuous_size,
            categorical_embedding_sizes=self.categorical_embedding_sizes,
            add_relative_index=self.add_relative_index,
            use_static_covariates=self.use_static_covariates,
            optimizer_kwargs=self.optimizer_kwargs,
            pl_trainer_kwargs=self.pl_trainer_kwargs,
            random_state=self.random_state,
            **self.kwargs,
        )

        self.model.fit(
            targets,
            past_covariates=past_covariates,
            future_covariates=future_covariates,
        )

        self._is_trained = True
        self.data_schema = data_schema
        self.targets_series = targets
        self.past_covariates = past_covariates
        self.training_future_covariates = future_covariates

    def predict(
        self, test_data: pd.DataFrame, prediction_col_name: str
    ) -> pd.DataFrame:
        """Make the forecast of given length.

        Args:
            test_data (pd.DataFrame): Given test input for forecasting.
            prediction_col_name (str): Name to give to prediction column.
        Returns:
            pd.DataFrame: The predictions dataframe.
        """
        if not self._is_trained:
            raise NotFittedError("Model is not fitted yet.")

        future_covariates = self._prepare_test_data(test_data)

        predictions = self.model.predict(
            n=self.data_schema.forecast_length,
            series=self.targets_series,
            past_covariates=self.past_covariates,
            future_covariates=future_covariates,
        )
        prediction_values = []
        for index, prediction in enumerate(predictions):
            prediction = prediction.pd_dataframe()
            values = prediction.values
            values = self.scalers[index].inverse_transform(values)
            prediction_values += list(values)

        test_data[prediction_col_name] = np.array(prediction_values)
        return test_data

    def save(self, model_dir_path: str) -> None:
        """Save the Forecaster to disk.

        Args:
            model_dir_path (str): Dir path to which to save the model.
        """
        if not self._is_trained:
            raise NotFittedError("Model is not fitted yet.")
        self.model.save(os.path.join(model_dir_path, MODEL_FILE_NAME))
        joblib.dump(self, os.path.join(model_dir_path, PREDICTOR_FILE_NAME))

    @classmethod
    def load(cls, model_dir_path: str) -> "Forecaster":
        """Load the Forecaster from disk.

        Args:
            model_dir_path (str): Dir path to the saved model.
        Returns:
            Forecaster: A new instance of the loaded Forecaster.
        """
        forecaster = joblib.load(os.path.join(model_dir_path, PREDICTOR_FILE_NAME))
        model = TFTModel.load(os.path.join(model_dir_path, MODEL_FILE_NAME))
        forecaster.model = model
        return forecaster

    def __str__(self):
        # sort params alphabetically for unit test to run successfully
        return f"Model name: {self.model_name}"


def train_predictor_model(
    history: pd.DataFrame,
    data_schema: ForecastingSchema,
    hyperparameters: dict,
) -> Forecaster:
    """
    Instantiate and train the predictor model.

    Args:
        history (pd.DataFrame): The training data inputs.
        data_schema (ForecastingSchema): Schema of the training data.
        hyperparameters (dict): Hyperparameters for the Forecaster.

    Returns:
        'Forecaster': The Forecaster model
    """

    model = Forecaster(
        data_schema=data_schema,
        **hyperparameters,
    )
    model.fit(
        history=history,
        data_schema=data_schema,
    )
    return model


def predict_with_model(
    model: Forecaster, test_data: pd.DataFrame, prediction_col_name: str
) -> pd.DataFrame:
    """
    Make forecast.

    Args:
        model (Forecaster): The Forecaster model.
        test_data (pd.DataFrame): The test input data for forecasting.
        prediction_col_name (int): Name to give to prediction column.

    Returns:
        pd.DataFrame: The forecast.
    """
    return model.predict(test_data, prediction_col_name)


def save_predictor_model(model: Forecaster, predictor_dir_path: str) -> None:
    """
    Save the Forecaster model to disk.

    Args:
        model (Forecaster): The Forecaster model to save.
        predictor_dir_path (str): Dir path to which to save the model.
    """
    if not os.path.exists(predictor_dir_path):
        os.makedirs(predictor_dir_path)
    model.save(predictor_dir_path)


def load_predictor_model(predictor_dir_path: str) -> Forecaster:
    """
    Load the Forecaster model from disk.

    Args:
        predictor_dir_path (str): Dir path where model is saved.

    Returns:
        Forecaster: A new instance of the loaded Forecaster model.
    """
    return Forecaster.load(predictor_dir_path)
