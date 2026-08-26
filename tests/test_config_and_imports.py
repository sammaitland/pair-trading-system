"""
Test that all modules import cleanly and configuration loads correctly.

These tests verify that an external viewer can install the package and
import every module without errors, even without a live broker connection.
"""

import importlib

import pytest


# ---------------------------------------------------------------------------
# Every module in src/ must import without error
# ---------------------------------------------------------------------------

SHARED_MODULES = [
    "src.shared.config",
    "src.shared.config_helper",
    "src.shared.constraints",
    "src.shared.scoring_constants",
]

CALIBRATION_MODULES = [
    "src.calibration.beta_estimator_interface",
    "src.calibration.reference_beta_estimator",
    "src.calibration.pair_generator_interface",
    "src.calibration.reference_pair_generator",
    "src.calibration.optimizer_interface",
    "src.calibration.reference_optimizer",
    "src.calibration.parameters_extraction",
    "src.calibration.percentile_saver",
]

SIGNAL_MODULES = [
    "src.signals.scoring_interface",
    "src.signals.reference_scorer",
    "src.signals.factor_shock_interface",
    "src.signals.reference_factor_shock",
]

EXECUTION_MODULES = [
    "src.execution.delisting_handler",
    "src.execution.execution_workflow",
    "src.execution.portfolio_management",
    "src.execution.reconciliation",
    "src.execution.stop_loss_protection",
    "src.execution.trade_execution",
    "src.execution.daily_data_capture",
]

IMPLEMENTATION_MODULES = [
    "src.implementation.pre_filter",
    "src.implementation.lam",
]

FIXTURE_MODULES = [
    "fixtures.synthetic_pairs",
]


@pytest.mark.parametrize("module", SHARED_MODULES + CALIBRATION_MODULES + SIGNAL_MODULES + EXECUTION_MODULES + IMPLEMENTATION_MODULES + FIXTURE_MODULES)
def test_module_imports(module):
    """Every listed module imports without raising."""
    importlib.import_module(module)


# ---------------------------------------------------------------------------
# Configuration accessor coverage
# ---------------------------------------------------------------------------

def test_config_loads_synthetic_values(synthetic_config):
    """Config accessors return the synthetic values from conftest."""
    from src.shared import config

    assert config.active_version() == "V9.3"
    assert config.trading_env() == "paper"
    assert config.index_etfs() == ["VGT", "VFH", "VIS", "VHT", "VCR"]
    assert config.base_trade_size() == 10000
    assert config.max_account_leverage() == 2.0
    assert config.min_stock_price() == 1.0
    assert config.max_stock_price() == 10000.0


def test_config_version_parsing():
    """Version string parsing handles all expected formats."""
    from src.shared import config

    assert config.parse_version_from_model("V9.2_single_factor") == "V9.2"
    assert config.parse_version_from_model("9.2") == "V9.2"
    assert config.parse_version_from_model(None) == config.active_version()


def test_config_paths_are_nonempty(synthetic_config):
    """With synthetic config, path accessors return non-empty strings."""
    from src.shared import config

    assert config._base_dir() != ""
    assert config.implementation_dir() != ""
    assert config.cache_dir() != ""


def test_no_active_version_attribute():
    """Verify config.ACTIVE_VERSION attribute does not exist (it is a function)."""
    from src.shared import config

    assert not hasattr(config, "ACTIVE_VERSION"), (
        "config.ACTIVE_VERSION should not exist — use config.active_version()"
    )
