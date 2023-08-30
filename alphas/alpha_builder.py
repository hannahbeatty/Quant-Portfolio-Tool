"""
Using a pseudo-builder design pattern to make the creation of alphas more scalable
and efficient
"""
from __future__ import annotations

import os
import sys
from abc import ABC
from abc import abstractmethod
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

sys.path.append(os.getcwd())
sys.path.append(str(Path(os.getcwd()).parent.absolute()))

from tqdm import tqdm

from data import data_utils


class IFormulaicAlpha(ABC):
    """
    The builder interface that specifies the methods for creating a formulaic alpha
    """

    @abstractmethod
    def get_data(self) -> pd.DataFrame:
        """
        returns the data
        """
        raise NotImplementedError

    @abstractmethod
    def run(self) -> pd.DataFrame:
        """
        runs the strategy
        """
        raise NotImplementedError


class FormulaicAlphaBuilder(IFormulaicAlpha):
    """
    Follows the Builder interface
    """

    def __init__(
        self,
        alpha_func: Callable[[pd.DataFrame], pd.DataFrame],
        start_date: str,
        vol_target: float,
        get_data: bool = None,
    ) -> None:
        super(IFormulaicAlpha).__init__()

        self._alpha_func = alpha_func
        self.start_date = start_date
        self.vol_target = vol_target
        self.db_data, self.returns_data = get_data if get_data is not None else self.get_data()
        self.ma_pair = (20, 60)

        self.raw_signal = pd.DataFrame()
        self.ex_ante_vol = pd.DataFrame()
        self.votes = None
        self.positions = None
        self.weights = None
        self.gnv = None
        self.outlier_positions = None
        self.alpha_model_returns = None
        self.positions_vol_target = None

        self.run()

    def run(self) -> pd.DataFrame:
        """
        1) Generates intertemporal raw alpha signals for each instrument.
        2) Generates indicator positions (1, 0, -1) for each instrument based on
           the alpha signal.
        3) Conducts asset-level equal risk allocation volatilty targeting.
        4) Generates unconstrained simulated returns.
        5) Aggregates key alpha model portfolio statistics.
        """
        # Get raw alpha signal for each instrument in universe
        for _, (ticker, tmp_data) in tqdm(enumerate(self.db_data.items())):
            print(f"Getting formulaic alpha for {ticker}")

            # Update raw alpha signal
            self.raw_signal = pd.concat([self.raw_signal, self._alpha_func(tmp_data, ticker)], axis=1).sort_index()

            # Get ex-ante vol (default to 40% annualized vol) -- preferably import from
            # a pre-computed risk model
            default_vol = 0.40 / np.sqrt(252)
            self.ex_ante_vol = pd.concat(
                [
                    self.ex_ante_vol,
                    tmp_data["adj_close_returns"].rolling(20).std().rename(ticker).fillna(default_vol),
                ],
                axis=1,
            ).sort_index()

            # Update instrument's data
            self.db_data[ticker] = tmp_data

        # Get binary votes from alpha singal (here this is long only)
        self.votes = self.raw_signal.mask(self.raw_signal > 0, 1).mask(self.raw_signal <= 0, 0)

        # Get signal conviction -- once you have a multi-strategy system, you can create
        # a signal for each instrument that generates dynamic conviction levels in each
        # instrument
        # alpha_data['signal_strength'] = alpha_data['votes'].apply(lambda x: np.sum(x)
        # / len(x.dropna()), axis=1)

        # Asset level vol targeting (equal risk allocation)
        daily_vol_target = self.vol_target / np.sqrt(252)
        self.positions_vol_target = self.votes.apply(
            lambda x: np.abs(x) * daily_vol_target
            if isinstance(x, float)
            else np.abs(x) * daily_vol_target / np.sum(np.abs(x)),
            axis=1,
        )

        # Asset level vol scalars
        vol_scalars = self.positions_vol_target / self.ex_ante_vol

        # Nomial positions
        self.positions = self.votes * vol_scalars

        # Proportional weights (not nominal positions)
        self.weights = self.positions / np.abs(self.positions).sum(axis=1)

        # Gross notional value (leverage)
        self.gnv = np.abs(self.positions).sum(axis=1)

        # Summarize outlier positions
        indices, tickers = np.where(self.positions > self.positions.quantile(0.999).quantile(0.999))
        outlier_positions = self.positions.values[indices, tickers]
        outlier_tickers = self.positions.columns[tickers]
        outlier_indices = self.positions.index[indices]
        self.outlier_positions = pd.DataFrame(
            {"ticker": outlier_tickers, "positions": outlier_positions},
            index=outlier_indices,
        ).sort_index()

        # From here, incorporate alpha/strategy level vol scaling as a function of
        # realized volatilty. This would leverage a vol modeling algorithm to estimate
        # ex-ante vol of the portfolio, then scale all positions based on the proportion
        # to target strategy vol and ex-ante vol. This separates vol targeting between
        # the asset and strategy level. A more refined rendition on this is to create a
        # risk model that accounts for covariance matrix to capture cross-asset dynamics.

        # Get alpha model returns
        self.instrument_level_alpha_model_returns = (
            self.positions * self.returns_data.shift(-1)[self.positions.columns]
        ).iloc[:-1]
        self.alpha_model_returns = self.instrument_level_alpha_model_returns.sum(axis=1).rename("alpha_model_returns")

        # Number of alpha model views per instrument
        self.n_views = np.abs(self.votes).sum(axis=0).rename("n_views")

        # Naive volatility decomposition (i.e., this is not intertemporal risk
        # contribution which depends on covariance matrix... just an intrinsic
        # volatility decomposition)
        self.instrument_level_alpha_model_mean_vol = (
            self.instrument_level_alpha_model_returns.std() * 252**0.5
        ).rename("instrument_level_alpha_model_mean_vol")
        self.volatility_attribution = (
            self.instrument_level_alpha_model_mean_vol / self.instrument_level_alpha_model_mean_vol.sum()
        ).rename("volatility_attribution")

        # Naive performance attribution (returns should be distributed evenly if the
        # alpha captures diversifying effects)
        self.instrument_level_alpha_model_mean_return = self.instrument_level_alpha_model_returns.mean().rename(
            "instrument_level_alpha_model_mean_return"
        )
        self.performance_attribution = (
            self.instrument_level_alpha_model_mean_return / self.instrument_level_alpha_model_mean_return.sum()
        ).rename("performance_attribution")

        # Scale by square root of the NOBS to capture statistical significance of each
        # instruments' returns
        obs_scalars = np.sqrt(self.n_views)
        self.adjusted_performance_attribution = (
            (self.instrument_level_alpha_model_mean_return * obs_scalars)
            / (self.instrument_level_alpha_model_mean_return * obs_scalars).sum()
        ).rename("adjusted_performance_attribution")

        # Decompose cumulative gains
        final_cumulative_returns = ((1 + self.instrument_level_alpha_model_returns).cumprod() - 1).iloc[-1]
        self.cumulative_performance_attribution = (final_cumulative_returns / final_cumulative_returns.sum()).rename(
            "cumulative_performance_attribution"
        )

    def get_data(self) -> pd.DataFrame:
        """
        Loads cached database data and returns data.

        Returns:
            tuple: Database data and stock-level returns.
        """
        return data_utils.load_cache(r"data/sp_500/sp_500_cache.pickle"), data_utils.load_cache(
            r"data/sp_500/adj_close_returns.pickle"
        )
