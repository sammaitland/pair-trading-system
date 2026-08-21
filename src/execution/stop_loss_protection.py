"""
Stop loss protection module for short squeeze risk management.

Handles stop price calculation based on alpha deterioration thresholds,
stop order placement and updates, orphan detection (stopped-out shorts
leaving orphaned longs), and orphaned long position closure.

STATUS: live
"""

import sys
import os
import asyncio
import logging
from datetime import datetime
import pandas as pd
import numpy as np

from src.shared import config
from ib_insync import Stock, StopOrder, MarketOrder

logger = logging.getLogger(__name__)


# ============================================================================
# STOP ORDER TAG MANAGEMENT
# ============================================================================

def get_stop_order_tag(trade_tag):
    """
    Generate stop order reference tag from trade tag
    
    Parameters:
    -----------
    trade_tag : int or str
        Original trade tag from portfolio
    
    Returns:
    --------
    str: Stop order reference (e.g., "SQLOSS_3819")
    """
    return f"{config.stop_loss_tag_prefix()}{trade_tag}"


def parse_stop_order_tag(order_ref):
    """
    Extract trade tag from stop order reference
    
    Parameters:
    -----------
    order_ref : str
        Stop order reference (e.g., "SQLOSS_3819")
    
    Returns:
    --------
    str: Original trade tag, or None if not a stop order
    """
    if order_ref and order_ref.startswith(config.stop_loss_tag_prefix()):
        return order_ref[len(config.stop_loss_tag_prefix()):]
    return None


# ============================================================================
# LEG IDENTIFICATION
# ============================================================================

def get_short_leg_info(trade):
    """
    Determine which leg is short and return its details
    
    Parameters:
    -----------
    trade : Series or dict
        Trade/position data
    
    Returns:
    --------
    dict: {
        'ticker': str,           # Short leg ticker
        'entry_price': float,    # Entry price of short leg
        'quantity': int,         # Quantity (positive)
        'is_co1': bool          # True if short leg is Co1
    }
    """
    tail = trade.get('Tail', 'L').strip().upper()
    
    if tail == 'L':
        # L-tail: Long Co1, Short Co2
        return {
            'ticker': trade['Co2'],
            'entry_price': trade['Co2 at Initiation'],
            'quantity': abs(trade.get('Quantity2', 0)),
            'is_co1': False
        }
    else:
        # U-tail: Short Co1, Long Co2
        return {
            'ticker': trade['Co1'],
            'entry_price': trade['Co1 at Initiation'],
            'quantity': abs(trade.get('Quantity1', 0)),
            'is_co1': True
        }


def get_long_leg_info(trade):
    """
    Determine which leg is long and return its details
    
    Parameters:
    -----------
    trade : Series or dict
        Trade/position data
    
    Returns:
    --------
    dict: {
        'ticker': str,           # Long leg ticker
        'entry_price': float,    # Entry price of long leg
        'quantity': int,         # Quantity (positive)
        'is_co1': bool          # True if long leg is Co1
    }
    """
    tail = trade.get('Tail', 'L').strip().upper()
    
    if tail == 'L':
        # L-tail: Long Co1, Short Co2
        return {
            'ticker': trade['Co1'],
            'entry_price': trade['Co1 at Initiation'],
            'quantity': abs(trade.get('Quantity1', 0)),
            'is_co1': True
        }
    else:
        # U-tail: Short Co1, Long Co2
        return {
            'ticker': trade['Co2'],
            'entry_price': trade['Co2 at Initiation'],
            'quantity': abs(trade.get('Quantity2', 0)),
            'is_co1': False
        }


# ============================================================================
# STOP PRICE CALCULATION
# ============================================================================

def calculate_stop_price(trade, current_index_price=None):
    """
    Calculate stop loss price for short leg based on alpha deterioration threshold
    
    Formula:
        stop_price = entry_price * (1 + threshold + beta * index_return)
    
    This adjusts for market moves - if index rallied, stop adjusts up accordingly
    to avoid false triggers from broad market moves.
    
    Parameters:
    -----------
    trade : Series or dict
        Trade/position data with entry prices and beta
    current_index_price : float, optional
        Current sector index price. If None, uses entry price (index_return=0)
    
    Returns:
    --------
    float: Stop price for short leg, or None if calculation fails
    """
    try:
        short_info = get_short_leg_info(trade)
        entry_price = short_info['entry_price']
        
        if pd.isna(entry_price) or entry_price <= 0:
            logger.warning(f"Invalid entry price for {trade.get('Pair', 'Unknown')}")
            return None
        
        # Get index data
        index_at_entry = trade.get('Index at Initiation')
        
        # Calculate index return (0 if no current price provided)
        if current_index_price and index_at_entry and index_at_entry > 0:
            index_return = (current_index_price - index_at_entry) / index_at_entry
        else:
            index_return = 0.0
        
        # Get beta (use absolute value - we want magnitude of expected move)
        beta = abs(trade.get('Beta', 1.0))
        
        # Cap beta at reasonable value to avoid extreme stop adjustments
        beta = min(beta, 2.0)
        
        # Calculate stop price
        threshold = config.stop_loss_alpha_threshold()
        stop_multiplier = 1 + threshold + (beta * index_return)
        
        # Safety floor: stop should never be below entry price
        stop_multiplier = max(stop_multiplier, 1 + threshold * 0.5)
        
        stop_price = entry_price * stop_multiplier
        
        return round(stop_price, 2)
        
    except Exception as e:
        logger.error(f"Error calculating stop price for {trade.get('Pair', 'Unknown')}: {e}")
        return None


def calculate_all_stop_prices(portfolio_df, index_prices=None):
    """
    Calculate stop prices for all positions in portfolio
    
    Parameters:
    -----------
    portfolio_df : DataFrame
        Active portfolio
    index_prices : dict, optional
        Current sector index prices {index_ticker: price}
        e.g., {'VGT': 250.50, 'VIS': 180.25, ...}
    
    Returns:
    --------
    DataFrame: Portfolio with updated Stop_Price column
    """
    if portfolio_df.empty:
        return portfolio_df
    
    if index_prices is None:
        index_prices = {}
    
    portfolio_df = portfolio_df.copy()
    
    for idx, trade in portfolio_df.iterrows():
        # Get current index price for this trade's sector
        trade_index = trade.get('Index', 'VGT')
        current_index_price = index_prices.get(trade_index)
        
        # Handle if price is a dict (e.g., {'bid': x, 'ask': y, 'live_price': z})
        if isinstance(current_index_price, dict):
            current_index_price = (
                current_index_price.get('live_price') or 
                current_index_price.get('mid') or
                current_index_price.get('close')
            )
        
        # Ensure it's a valid float
        if current_index_price is not None:
            try:
                current_index_price = float(current_index_price)
            except (TypeError, ValueError):
                current_index_price = None
        
        # Calculate stop price
        stop_price = calculate_stop_price(trade, current_index_price)
        
        portfolio_df.at[idx, 'Stop_Price'] = stop_price
    
    # Log summary
    valid_stops = portfolio_df['Stop_Price'].notna().sum()
    logger.info(f"Calculated stop prices for {valid_stops}/{len(portfolio_df)} positions")
    
    return portfolio_df


# ============================================================================
# STOP ORDER MANAGEMENT
# ============================================================================

async def get_existing_stop_orders(ib):
    """
    Get all existing stop orders placed by this system
    
    Returns:
    --------
    dict: {trade_tag: order_info}
    """
    stop_orders = {}
    
    try:
        open_orders = ib.openOrders()
        
        for order in open_orders:
            order_ref = order.orderRef if hasattr(order, 'orderRef') else None
            
            if order_ref and order_ref.startswith(config.stop_loss_tag_prefix()):
                trade_tag = parse_stop_order_tag(order_ref)
                
                if trade_tag:
                    stop_orders[trade_tag] = {
                        'order_id': order.orderId,
                        'order': order,
                        'order_ref': order_ref
                    }
                    logger.debug(f"Found existing stop order: {order_ref} (ID: {order.orderId})")
        
        logger.info(f"Found {len(stop_orders)} existing stop orders")
        return stop_orders
        
    except Exception as e:
        logger.error(f"Error fetching open orders: {e}")
        return {}


async def cancel_stop_order(ib, order_id, order_ref=""):
    """
    Cancel a stop order by ID
    
    Returns:
    --------
    bool: True if cancelled successfully
    """
    try:
        for trade in ib.openTrades():
            if trade.order.orderId == order_id:
                ib.cancelOrder(trade.order)
                await asyncio.sleep(0.5)
                logger.info(f"  Cancelled stop order {order_ref} (ID: {order_id})")
                return True
        
        logger.warning(f"Could not find order ID {order_id} to cancel")
        return False
        
    except Exception as e:
        logger.error(f"Error cancelling order {order_id}: {e}")
        return False


async def place_stop_order(ib, ticker, quantity, stop_price, trade_tag):
    """
    Place a stop order for a short position
    
    Parameters:
    -----------
    ib : IB
        Connected IB instance
    ticker : str
        Stock ticker
    quantity : int
        Number of shares (positive - order will be BUY to cover)
    stop_price : float
        Stop trigger price
    trade_tag : str or int
        Original trade tag for reference
    
    Returns:
    --------
    int: Order ID if successful, None otherwise
    """
    # SAFETY CHECK: Don't place stops if disabled in config
    if not config.execute_stops():
        logger.info(f"⚠️  Stop execution DISABLED - skipping stop order for {ticker} (Tag: {trade_tag})")
        return None
    
    try:
        if quantity <= 0:
            logger.error(f"Invalid quantity for {ticker}: {quantity}")
            return None
        
        if stop_price <= 0:
            logger.error(f"Invalid stop price for {ticker}: {stop_price}")
            return None
        
        contract = Stock(ticker, 'SMART', 'USD')
        qualified = await ib.qualifyContractsAsync(contract)
        
        if not qualified:
            logger.error(f"Could not qualify contract for {ticker}")
            return None
        
        order_ref = get_stop_order_tag(trade_tag)
        
        order = StopOrder(
            action='BUY',
            totalQuantity=quantity,
            stopPrice=stop_price,
            orderRef=order_ref,
            tif='GTC'
        )
        
        trade = ib.placeOrder(qualified[0], order)
        await asyncio.sleep(1)
        
        if trade.order.orderId:
            logger.info(f"Placed stop order: BUY {quantity} {ticker} @ ${stop_price:.2f} "
                       f"(Ref: {order_ref}, ID: {trade.order.orderId})")
            return trade.order.orderId
        else:
            logger.error(f"Failed to get order ID for {ticker} stop order")
            return None
        
    except Exception as e:
        logger.error(f"Error placing stop order for {ticker}: {e}")
        return None


async def update_stop_losses(ib, portfolio_df, index_prices, verbose=True):
    """
    Update stop loss orders for all positions
    
    Parameters:
    -----------
    ib : IB
        Connected IB instance
    portfolio_df : DataFrame
        Active portfolio
    index_prices : dict
        Current index prices {ticker: price}
    verbose : bool
        Print detailed output
    
    Returns:
    --------
    dict: Summary of actions taken
    """
    result = {
        'stops_cancelled': 0,
        'stops_placed': 0,
        'stops_updated': 0,
        'errors': []
    }
    
    if not config.enable_short_squeeze_protection():
        if verbose:
            print("  Stop loss protection is DISABLED")
        return result
    
    # ADD THIS NEW CHECK:
    if not config.execute_stops():
        if verbose:
            print("  ⚠️  Stop execution is DISABLED in config - skipping all stop orders")
        return result
    
    if portfolio_df.empty:
        if verbose:
            print("  No positions to protect")
        return result
    
    # Get existing stops
    existing_stops = await get_existing_stop_orders(ib)
    
    if verbose:
        print(f"  Found {len(existing_stops)} existing stop orders")
    
    # Process each position
    for idx, trade in portfolio_df.iterrows():
        trade_tag = str(trade['Tag'])
        pair = trade.get('Pair', 'Unknown')
        
        # Get index price for this trade's sector
        trade_index = trade.get('Index', 'VGT')
        current_index_price = index_prices.get(trade_index)
        
        # Calculate stop price
        stop_price = calculate_stop_price(trade, current_index_price)
        
        if stop_price is None or stop_price <= 0:
            continue
        
        # Get short leg info
        short_info = get_short_leg_info(trade)
        short_ticker = short_info['ticker']
        short_qty = short_info['quantity']
        
        if short_qty <= 0:
            continue
        
        # Cancel existing stop if present
        if trade_tag in existing_stops:
            old_order = existing_stops[trade_tag]
            cancelled = await cancel_stop_order(
                ib, 
                old_order['order_id'],
                old_order['order_ref']
            )
            if cancelled:
                result['stops_cancelled'] += 1
            await asyncio.sleep(0.3)
        
        # Place new stop order
        order_id = await place_stop_order(
            ib,
            short_ticker,
            short_qty,
            stop_price,
            trade_tag
        )
        
        if order_id:
            portfolio_df.at[idx, 'Stop_Order_ID'] = order_id
            portfolio_df.at[idx, 'Stop_Price'] = stop_price
            result['stops_placed'] += 1
            result['stops_updated'] += 1
            
            if verbose:
                print(f"    {pair}: {short_ticker} stop @ ${stop_price:.2f} (ID: {order_id})")
        else:
            result['errors'].append(f"Failed to place stop for {pair}")
    
    return result


# ============================================================================
# ORPHAN DETECTION
# ============================================================================

async def detect_stop_loss_orphans(portfolio_df, ib):
    """
    Detect positions where short leg stop was triggered, leaving orphaned long
    
    Compares TWS positions against portfolio file. If a short position exists
    in portfolio but not in TWS, the stop was likely triggered.
    
    Parameters:
    -----------
    portfolio_df : DataFrame
        Current portfolio from file
    ib : IB
        Connected IB instance
    
    Returns:
    --------
    tuple: (orphaned_trades_df, remaining_df)
        orphaned_trades_df: Trades where short was stopped out
        remaining_df: Trades still intact
    """
    # ADD THIS CHECK:
    if not config.execute_stops():
        logger.info("⚠️  Stop execution DISABLED - orphan detection still running but will not close positions")
    
    if portfolio_df.empty:
        return pd.DataFrame(), portfolio_df
    
    if ib is None or not ib.isConnected():
        logger.warning("No IBKR connection - cannot detect orphans")
        return pd.DataFrame(), portfolio_df
    
    logger.info("Checking for stop loss orphans...")
    
    # Get TWS positions
    tws_positions = {}
    for position in ib.positions():
        contract = position.contract
        if contract.secType == 'STK':
            tws_positions[contract.symbol] = position.position
    
    logger.info(f"  TWS has {len(tws_positions)} stock positions")
    
    orphaned_trades = []
    remaining_trades = []
    
    for idx, trade in portfolio_df.iterrows():
        pair = trade.get('Pair', 'Unknown')
        tag = trade.get('Tag')
        tail = trade.get('Tail', 'L').strip().upper()
        
        # Determine short and long legs
        if tail == 'L':
            short_ticker = trade['Co2']
            short_qty_expected = -abs(trade.get('Quantity2', 0))
            long_ticker = trade['Co1']
            long_qty_expected = abs(trade.get('Quantity1', 0))
        else:
            short_ticker = trade['Co1']
            short_qty_expected = -abs(trade.get('Quantity1', 0))
            long_ticker = trade['Co2']
            long_qty_expected = abs(trade.get('Quantity2', 0))
        
        # Get actual TWS positions
        tws_short_qty = tws_positions.get(short_ticker, 0)
        tws_long_qty = tws_positions.get(long_ticker, 0)
        
        # Check if short position is missing or significantly reduced
        short_missing = False
        
        if short_qty_expected < 0:
            expected_short_size = abs(short_qty_expected)
            actual_short_size = abs(min(tws_short_qty, 0))
            
            # Consider short "missing" if less than 20% of expected remains
            if actual_short_size < expected_short_size * 0.2:
                short_missing = True
                logger.info(f"  {pair}: Short {short_ticker} missing/reduced "
                           f"(expected: {short_qty_expected}, TWS: {tws_short_qty})")
        
        if short_missing:
            # Check if long is still there (confirming orphan, not full exit)
            if tws_long_qty > 0:
                logger.warning(f"  🚨 ORPHAN DETECTED: {pair} (Tag: {tag})")
                logger.warning(f"     Short {short_ticker} stopped out, Long {long_ticker} remains")
                
                trade_copy = trade.copy()
                trade_copy['Exit Reason'] = f'Stop Loss - Short {short_ticker} Exited'
                trade_copy['Trade Termination Date'] = datetime.today().date()
                orphaned_trades.append(trade_copy)
            else:
                # Both legs gone - probably manual close
                logger.info(f"  {pair}: Both legs closed (manual exit?)")
                remaining_trades.append(trade)
        else:
            remaining_trades.append(trade)
    
    orphaned_df = pd.DataFrame(orphaned_trades) if orphaned_trades else pd.DataFrame()
    remaining_df = pd.DataFrame(remaining_trades) if remaining_trades else pd.DataFrame()
    
    if len(orphaned_trades) > 0:
        logger.warning(f"⚠️  Found {len(orphaned_trades)} orphaned positions from stop losses")
    else:
        logger.info("✓ No stop loss orphans detected")
    
    return orphaned_df, remaining_df


async def close_orphaned_long(ib, trade, live_prices=None):
    """
    Close the long leg of an orphaned trade
    
    Parameters:
    -----------
    ib : IB
        Connected IB instance
    trade : Series
        Trade data with orphaned long position
    live_prices : dict, optional
        Current prices for logging
    
    Returns:
    --------
    bool: True if successfully closed
    """
    # SAFETY CHECK: Don't close orphans if stop execution disabled
    if not config.execute_stops():
        logger.info(f"⚠️  Stop execution DISABLED - skipping orphan closure for {trade.get('Pair')}")
        return False
    
    long_info = get_long_leg_info(trade)
    long_ticker = long_info['ticker']
    long_qty = long_info['quantity']
    
    if long_qty <= 0:
        logger.warning(f"No quantity to close for {trade.get('Pair')}")
        return False
    
    try:
        contract = Stock(long_ticker, 'SMART', 'USD')
        qualified = ib.qualifyContracts(contract)
        
        if not qualified:
            logger.error(f"Could not qualify {long_ticker}")
            return False
        
        order = MarketOrder('SELL', long_qty)
        trade_obj = ib.placeOrder(qualified[0], order)
        
        logger.info(f"  Placed order: SELL {long_qty} {long_ticker}")
        
        # Wait for fill (up to 30 seconds)
        for i in range(30):
            ib.sleep(1)
            if trade_obj.orderStatus.status == 'Filled':
                fill_price = trade_obj.orderStatus.avgFillPrice
                logger.info(f"  ✓ Filled: {long_qty} {long_ticker} @ ${fill_price:.2f}")
                return True
            elif trade_obj.orderStatus.status in ['Cancelled', 'Rejected']:
                logger.error(f"  Order {trade_obj.orderStatus.status}")
                return False
        
        # Timeout
        if trade_obj.orderStatus.filled > 0:
            logger.warning(f"  Partial fill: {trade_obj.orderStatus.filled}/{long_qty}")
            return True
        else:
            logger.error(f"  Order timeout - cancelling")
            ib.cancelOrder(trade_obj.order)
            return False
        
    except Exception as e:
        logger.error(f"Error closing orphaned long {long_ticker}: {e}")
        return False


# ============================================================================
# VALIDATION
# ============================================================================

def validate_stop_loss_config():
    """
    Validate stop loss configuration at startup
    
    Returns:
    --------
    bool: True if configuration is valid
    """
    if not config.enable_short_squeeze_protection():
        logger.info("Short squeeze protection is DISABLED")
        return True
    
    threshold = config.stop_loss_alpha_threshold()
    
    if threshold <= 0 or threshold > 1.0:
        logger.error(f"Invalid STOP_LOSS_ALPHA_THRESHOLD: {threshold} (must be 0 < x <= 1.0)")
        return False
    
    if threshold < 0.20:
        logger.warning(f"STOP_LOSS_ALPHA_THRESHOLD={threshold:.0%} is quite tight - may trigger frequently")
    
    logger.info(f"Short squeeze protection ENABLED: {threshold:.0%} alpha threshold")
    return True