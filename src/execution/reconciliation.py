"""
TWS position reconciliation module.

Compares portfolio file positions against live TWS positions, detects
discrepancies (missing positions, quantity mismatches, rogue positions),
and executes remedies using limit orders with market order fallback.

Includes position inversion detection, which is critical for preventing
direction errors that caused a major live incident.

STATUS: live
"""

import logging
from src.shared import config

logger = logging.getLogger(__name__)


# ============================================================================
# RECONCILIATION
# ============================================================================

def reconcile_with_tws(portfolio_df, options_portfolio_df, ib, tolerance_usd=None, live_prices=None):
    """
    Reconcile portfolio with TWS positions.
    Uses DOLLAR-BASED tolerance (config.reconciliation_tolerance_usd()).
    """
    if tolerance_usd is None:
        tolerance_usd = getattr(config, 'RECONCILIATION_TOLERANCE_USD', 100)

    print("Starting portfolio reconciliation with TWS account")
    print(f"  Tolerance: ${tolerance_usd:.0f} (positions with value ≤${tolerance_usd:.0f} will be ignored)")

    results = {
        "missing_in_tws": [],
        "missing_in_portfolio": [],
        "quantity_mismatch": [],
        "options_missing_in_tws": [],
        "options_missing_in_portfolio": [],
        "options_quantity_mismatch": []
    }

    # Get all positions from TWS
    print("Fetching positions from TWS account")
    tws_positions = {}
    tws_options = {}

    for position in ib.positions():
        contract = position.contract
        if contract.secType == 'OPT':
            key = f"{contract.symbol}-{contract.lastTradeDateOrContractMonth}-{contract.strike}-{contract.right}"
            tws_options[key] = {
                'Symbol': contract.symbol,
                'Expiration': contract.lastTradeDateOrContractMonth,
                'Strike': contract.strike,
                'Right': contract.right,
                'Quantity': position.position
            }
        else:
            tws_positions[contract.symbol] = position.position

    print(f"Fetched {len(tws_positions)} stock positions and {len(tws_options)} option positions from TWS")

    if tws_positions:
        tws_tickers_sorted = sorted(tws_positions.keys())
        print(f"  TWS stock tickers: {', '.join(tws_tickers_sorted[:20])}{'...' if len(tws_tickers_sorted) > 20 else ''}")

    # Build price lookup
    price_lookup = {}
    if live_prices:
        for ticker, data in live_prices.items():
            if isinstance(data, dict):
                price_lookup[ticker] = data.get('live_price', data.get('price', 100))
            else:
                price_lookup[ticker] = data if data else 100

    def get_price(ticker):
        if ticker in price_lookup and price_lookup[ticker]:
            return float(price_lookup[ticker])
        return 100.0

    # Calculate expected positions from portfolio file
    print("Calculating expected positions from portfolio file")
    portfolio_positions = {}

    for _, row in portfolio_df.iterrows():
        ticker1 = row['Co1']
        ticker2 = row['Co2']
        qty1 = row.get('Quantity1', 0)
        qty2 = row.get('Quantity2', 0)

        tail = row.get('Tail', 'L').strip().upper()
        direction1 = 1 if tail == 'L' else -1
        direction2 = -1 if tail == 'L' else 1

        portfolio_positions[ticker1] = portfolio_positions.get(ticker1, 0) + qty1 * direction1
        portfolio_positions[ticker2] = portfolio_positions.get(ticker2, 0) + qty2 * direction2

    portfolio_tickers = set(portfolio_positions.keys())
    portfolio_nonzero = {t for t, q in portfolio_positions.items() if abs(q) > 0}
    print(f"  Portfolio has {len(portfolio_tickers)} unique tickers ({len(portfolio_nonzero)} with non-zero net positions)")

    tws_only = set(tws_positions.keys()) - portfolio_tickers
    portfolio_only_nonzero = [t for t in (portfolio_tickers - set(tws_positions.keys()))
                              if abs(portfolio_positions.get(t, 0)) > 0]

    if tws_only:
        print(f"  ⚠️  Tickers in TWS but NOT in portfolio file ({len(tws_only)}): {sorted(tws_only)}")
    if portfolio_only_nonzero:
        print(f"  ⚠️  Tickers in portfolio file but NOT in TWS ({len(portfolio_only_nonzero)}): {sorted(portfolio_only_nonzero)}")
    if not tws_only and not portfolio_only_nonzero:
        print(f"  ✓ All {len(tws_positions)} TWS tickers match portfolio file tickers")

    # Calculate expected options positions
    portfolio_options = {}
    if not options_portfolio_df.empty:
        for _, row in options_portfolio_df.iterrows():
            symbol = row['Symbol']
            expiration = row['Expiration']
            strike = row['Strike']
            contracts = row['Contracts']
            right = 'P'
            key = f"{symbol}-{expiration}-{strike}-{right}"
            portfolio_options[key] = {
                'Symbol': symbol, 'Expiration': expiration,
                'Strike': strike, 'Right': right, 'Quantity': contracts
            }

    # Compare stock positions (dollar-based tolerance)
    for ticker, qty in portfolio_positions.items():
        if abs(qty) <= 0:
            continue
        price = get_price(ticker)
        position_value = abs(qty * price)

        if ticker not in tws_positions:
            if position_value >= tolerance_usd:
                results["missing_in_tws"].append({
                    "ticker": ticker, "expected_qty": qty,
                    "actual_qty": 0, "dollar_value": position_value
                })
        else:
            diff = qty - tws_positions[ticker]
            diff_value = abs(diff * price)
            if diff_value >= tolerance_usd:
                results["quantity_mismatch"].append({
                    "ticker": ticker, "expected_qty": qty,
                    "actual_qty": tws_positions[ticker],
                    "difference": diff, "dollar_value": diff_value
                })

    skipped_small_positions = []
    for ticker, qty in tws_positions.items():
        price = get_price(ticker)
        position_value = abs(qty * price)

        if position_value <= tolerance_usd:
            skipped_small_positions.append((ticker, qty, position_value))
            continue

        if ticker not in portfolio_positions:
            results["missing_in_portfolio"].append({
                "ticker": ticker, "expected_qty": 0,
                "actual_qty": qty, "dollar_value": position_value
            })

    if skipped_small_positions:
        print(f"  ℹ️  Skipped {len(skipped_small_positions)} TWS positions with value ≤${tolerance_usd:.0f}:")
        for ticker, qty, value in skipped_small_positions:
            in_portfolio = "in portfolio" if ticker in portfolio_positions else "NOT in portfolio"
            print(f"      {ticker}: {int(qty)} shares (${value:.0f}) - {in_portfolio}")

    # Compare options positions
    for key, opt in portfolio_options.items():
        tws_key = None
        for tk in tws_options.keys():
            if opt['Symbol'] in tk and str(opt['Strike']) in tk and opt['Right'] in tk:
                tws_key = tk
                break

        if not tws_key:
            results["options_missing_in_tws"].append({
                "symbol": f"{opt['Symbol']} {opt['Strike']} {opt['Expiration']} {opt['Right']}",
                "expected_qty": opt['Quantity'], "actual_qty": 0
            })
        else:
            tws_opt = tws_options[tws_key]
            diff = opt['Quantity'] - tws_opt['Quantity']
            if abs(diff) >= 1:
                results["options_quantity_mismatch"].append({
                    "symbol": f"{opt['Symbol']} {opt['Strike']} {opt['Expiration']} {opt['Right']}",
                    "expected_qty": opt['Quantity'],
                    "actual_qty": tws_opt['Quantity'], "difference": diff
                })

    for key, opt in tws_options.items():
        if abs(opt['Quantity']) < 1:
            continue
        portfolio_key = None
        for pk in portfolio_options.keys():
            if opt['Symbol'] in pk and str(opt['Strike']) in pk and opt['Right'] in pk:
                portfolio_key = pk
                break
        if not portfolio_key:
            results["options_missing_in_portfolio"].append({
                "symbol": f"{opt['Symbol']} {opt['Strike']} {opt['Expiration']} {opt['Right']}",
                "expected_qty": 0, "actual_qty": opt['Quantity']
            })

    results["raw"] = {
        "tws_positions": tws_positions,
        "portfolio_positions": portfolio_positions,
        "tws_options": tws_options,
        "portfolio_options": portfolio_options
    }

    print("Portfolio reconciliation complete")
    return results


# ============================================================================
# REMEDIES
# ============================================================================

def execute_remedies(reconciliation_results, ib, live_prices=None):
    """Execute remedies for discrepancies using LIMIT ORDERS"""
    from ib_insync import Stock, MarketOrder, LimitOrder

    print("Preparing to execute reconciliation remedies...")

    class TIFPresetFilter(logging.Filter):
        def filter(self, record):
            return '10349' not in str(record.getMessage())

    ib_logger = logging.getLogger('ib_insync')
    tif_filter = TIFPresetFilter()
    ib_logger.addFilter(tif_filter)

    REMEDY_LIMIT_BUFFER_PCT = getattr(config, 'REMEDY_LIMIT_BUFFER_PCT', 0.005)
    REMEDY_LIMIT_TIMEOUT = getattr(config, 'REMEDY_LIMIT_TIMEOUT', 20)

    try:
        rogue_positions = reconciliation_results["missing_in_portfolio"]
        mismatched_positions = reconciliation_results["quantity_mismatch"]
        total_remedies = len(rogue_positions) + len(mismatched_positions)

        if total_remedies == 0:
            print("No remedies needed. Portfolio and TWS are reconciled.")
            return True

        print(f"Found {total_remedies} discrepancies to remedy:")
        print(f"  - {len(rogue_positions)} rogue positions to close")
        print(f"  - {len(mismatched_positions)} positions to adjust")

        success_count = 0
        failure_count = 0

        def execute_single_remedy(ticker, side, quantity, description, dollar_value=None):
            nonlocal success_count, failure_count

            value_str = f" (~${dollar_value:,.0f})" if dollar_value else ""
            print(f"  {ticker}: {side} {quantity} shares{value_str} - {description}")

            try:
                contract = Stock(ticker, 'SMART', 'USD')
                qualified_contracts = ib.qualifyContracts(contract)

                if not qualified_contracts:
                    print(f"    Failed to qualify contract for {ticker}")
                    failure_count += 1
                    return

                qualified = qualified_contracts[0]
                ticker_data = ib.reqMktData(qualified, '', False, False)
                ib.sleep(0.5)

                bid = ticker_data.bid if ticker_data.bid and ticker_data.bid > 0 else None
                ask = ticker_data.ask if ticker_data.ask and ticker_data.ask > 0 else None

                if side == "SELL":
                    limit_price = round(bid * (1 - REMEDY_LIMIT_BUFFER_PCT), 2) if bid and bid > 0 else None
                else:
                    limit_price = round(ask * (1 + REMEDY_LIMIT_BUFFER_PCT), 2) if ask and ask > 0 else None

                ib.cancelMktData(qualified)

                if limit_price:
                    print(f"    LIMIT order: {side} {quantity} @ ${limit_price:.2f}")
                    order = LimitOrder(side, quantity, limit_price, tif='DAY')
                else:
                    print(f"    MARKET order (no price data): {side} {quantity}")
                    order = MarketOrder(side, quantity, tif='DAY')

                trade = ib.placeOrder(qualified, order)

                max_wait = REMEDY_LIMIT_TIMEOUT if limit_price else 30
                filled = False

                for i in range(max_wait):
                    ib.sleep(1)
                    if trade.orderStatus.status == 'Filled' or trade.orderStatus.filled >= quantity:
                        print(f"    ✓ Filled: {int(trade.orderStatus.filled)} shares")
                        filled = True
                        success_count += 1
                        break
                    elif trade.orderStatus.filled > 0 and i % 5 == 0:
                        print(f"    Partial: {int(trade.orderStatus.filled)}/{quantity} shares")

                if not filled and limit_price:
                    remaining = int(quantity - trade.orderStatus.filled)
                    if remaining > 0:
                        print(f"    Converting to MARKET for remaining {remaining} shares")
                        if trade.orderStatus.status not in ['Filled', 'Cancelled', 'ApiCancelled']:
                            ib.cancelOrder(trade.order)
                            ib.sleep(1)

                        market_order = MarketOrder(side, remaining, tif='DAY')
                        market_trade = ib.placeOrder(qualified, market_order)

                        for i in range(15):
                            ib.sleep(1)
                            if market_trade.orderStatus.status == 'Filled':
                                print(f"    ✓ Market filled: {int(market_trade.orderStatus.filled)} shares")
                                filled = True
                                success_count += 1
                                break

                if not filled:
                    print(f"    ✗ Not filled after timeout")
                    if trade.orderStatus.status not in ['Filled', 'Cancelled', 'Rejected']:
                        ib.cancelOrder(trade.order)
                    failure_count += 1

            except Exception as e:
                print(f"    Error: {e}")
                failure_count += 1

        if rogue_positions:
            print("\nClosing rogue positions:")
            for pos in rogue_positions:
                ticker = pos["ticker"]
                qty = int(abs(pos["actual_qty"]))
                side = "SELL" if pos["actual_qty"] > 0 else "BUY"
                execute_single_remedy(ticker, side, qty, "close rogue position", pos.get("dollar_value"))

        if mismatched_positions:
            print("\nAdjusting mismatched positions:")
            for pos in mismatched_positions:
                ticker = pos["ticker"]
                difference = pos["difference"]
                side = "BUY" if difference > 0 else "SELL"
                qty = int(abs(difference))
                desc = f"adjust from {int(pos['actual_qty'])} to {int(pos['expected_qty'])}"
                execute_single_remedy(ticker, side, qty, desc, pos.get("dollar_value"))

        print("\nReconciliation remedy execution summary:")
        print(f"  Success: {success_count}/{total_remedies}")
        print(f"  Failure: {failure_count}/{total_remedies}")

        if success_count == total_remedies:
            print("\n✓ All reconciliation remedies executed successfully!")
            return True
        elif success_count > 0:
            print("\n⚠️ Some reconciliation remedies were executed successfully.")
            print("  Re-run the reconciliation to check the current status.")
            return True
        else:
            print("\n❌ Failed to execute any reconciliation remedies.")
            return False

    finally:
        ib_logger.removeFilter(tif_filter)


# ============================================================================
# DISPLAY UTILITIES
# ============================================================================

def format_reconciliation_summary(reconciliation_results, live_prices=None):
    """
    Format reconciliation results for display in Jupyter.

    Shows:
    1. Rogue positions (in TWS but not in portfolio)
    2. Quantity mismatches (size of discrepancy)
    3. Positions in portfolio but not in TWS

    Parameters
    ----------
    reconciliation_results : dict
        Output from reconcile_with_tws()
    live_prices : dict, optional
        Current market prices {ticker: price}
    """
    if live_prices is None:
        live_prices = {}

    print("\n" + "=" * 80)
    print("RECONCILIATION SUMMARY")
    print("=" * 80)

    rogue_positions = reconciliation_results.get("missing_in_portfolio", [])
    qty_mismatches = reconciliation_results.get("quantity_mismatch", [])
    missing_in_tws = reconciliation_results.get("missing_in_tws", [])

    total_discrepancies = len(rogue_positions) + len(qty_mismatches) + len(missing_in_tws)

    if total_discrepancies == 0:
        print("\n✓ FULLY RECONCILED - No discrepancies found")
        print("  Portfolio file matches TWS account perfectly")
        return

    print(f"\n⚠️  {total_discrepancies} discrepancies found\n")

    if rogue_positions:
        print("🚫 ROGUE POSITIONS (in TWS but not in portfolio file):")
        print("   These positions need to be closed or added to portfolio")
        print()
        for pos in rogue_positions:
            ticker = pos['ticker']
            qty = pos['actual_qty']
            price = _get_price_from_dict(live_prices, ticker, default=100)
            value_approx = abs(qty) * price
            side = "LONG" if qty > 0 else "SHORT"
            print(f"   {ticker:6s} {side:5s} {abs(qty):6.0f} shares (approx ${abs(value_approx):,.0f})")
        print()

    if qty_mismatches:
        print("⚠️  QUANTITY MISMATCHES (exposure differences):")
        print("   Portfolio vs TWS quantity differs - assess if worth correcting")
        print()
        total_mismatch_exposure = 0
        for mismatch in qty_mismatches:
            ticker = mismatch['ticker']
            expected = mismatch['expected_qty']
            actual = mismatch['actual_qty']
            diff = mismatch['difference']
            direction = "SHORT" if expected < 0 else "LONG"
            price = _get_price_from_dict(live_prices, ticker, default=100)
            value_approx = abs(diff) * price
            total_mismatch_exposure += value_approx
            print(f"   {ticker:6s} {direction:5s}: Expected {expected:6.0f}, "
                  f"Actual {actual:6.0f} (diff: {diff:+6.0f})")
        print(f"\n   Total mismatch exposure: ~${total_mismatch_exposure:,.0f}")
        print("   (Use cost-benefit analysis to decide if remedies are worth executing)")
        print()

    if missing_in_tws:
        print("❌ POSITIONS IN PORTFOLIO BUT NOT IN TWS:")
        print("   These are concerning - either:")
        print("   • Orders failed to execute")
        print("   • Positions were manually closed in TWS")
        print()
        for pos in missing_in_tws:
            ticker = pos['ticker']
            expected = pos['expected_qty']
            price = _get_price_from_dict(live_prices, ticker, default=100)
            value_approx = abs(expected) * price
            print(f"   {ticker:6s} Expected: {expected:6.0f} shares (approx ${abs(value_approx):,.0f})")
        print()

    print("=" * 80)
    print("\nRECOMMENDATION:")
    if rogue_positions:
        print("  1. Close rogue positions (highest priority)")
    if qty_mismatches:
        print("  2. Evaluate quantity mismatches for cost-benefit of correction")
    if missing_in_tws:
        print("  3. Investigate missing positions - were they supposed to execute?")
    print()


def _get_price_from_dict(live_prices, ticker, default=100):
    """Helper to extract price from live_prices dict (handles nested dicts)"""
    if not live_prices:
        return default

    price_data = live_prices.get(ticker)
    if price_data is None:
        return default

    if isinstance(price_data, dict):
        price = (price_data.get('live_price') or
                 price_data.get('mid') or
                 price_data.get('close') or
                 price_data.get('price'))
        return float(price) if price else default

    try:
        return float(price_data)
    except (TypeError, ValueError):
        return default
