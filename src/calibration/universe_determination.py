"""
Universe determination module for ETF holdings analysis.

Retrieves ETF holdings from Alpha Vantage, enriches them with market cap and
industry data, classifies tickers into consolidated categories, detects megacap
stocks, and saves the resulting universe to Excel. Supports VOX integration
into VGT with proper weight recalculation.

Exclusions: crypto proxy tickers, mREIT tickers, and tickers with no API data.

STATUS: live
"""

import requests
import pandas as pd
from datetime import datetime
import json
import time
import os
import random
from openpyxl import Workbook
from openpyxl.utils.dataframe import dataframe_to_rows
import numpy as np

from src.shared import config

# =============================================================================
# VERSION CONFIGURATION
# =============================================================================

VERSION = config.active_version()
VERSION_DIR = config.get_version_dir(VERSION)

# Configuration - Updated to include VOX for VGT with proper weight calculation
API_KEY = config.get("api_keys.alphavantage", "")
ETF_TICKERS = ['VGT', 'VHT', 'VFH', 'VIS', 'VCR']  # Configure which ETFs to analyze
OUTPUT_PATH = (VERSION_DIR)
OUTPUT_FILENAME = f'{VERSION}_All_Vanguard_ETF_Tickers.xlsx'

# Create output directory if it doesn't exist
if OUTPUT_PATH:
    os.makedirs(OUTPUT_PATH, exist_ok=True)

# VOX integration for VGT with proper weight recalculation
VOX_INTEGRATION = {
    'VGT': {
        'integrate_etf': 'VOX',
        'recalculate_weights': True,  # Enable proper weight calculation
        'weight_method': 'market_cap'  # 'market_cap' or 'equal_weight'
    }
}

# =============================================================================
# EXCLUSION LISTS
# =============================================================================

# Hard exclusion list - crypto proxy tickers
CRYPTO_TICKERS = config.crypto_tickers()

# mREIT exclusion - these are rate proxies, not suitable for pairs trading
MREIT_EXCLUSION = True
MREIT_TICKERS = config.mreit_tickers()

# =============================================================================
# CONSOLIDATED CATEGORY STRUCTURE
# =============================================================================
# Based on correlation analysis - economically-motivated consolidation

CONSOLIDATED_CATEGORIES = {
    'VGT': {
        'categories': ['Tech_Core', 'Semiconductors_AI'],
        'description': 'Tech broadly moves together except semiconductors which have distinct cycle exposure'
    },
    'VHT': {
        'categories': ['Biotech', 'Large_Pharma_Services', 'Medical_Devices_LifeSci'],
        'description': 'Biotech is idiosyncratic, pharma/services more stable, devices/lifesci capital goods-like'
    },
    'VFH': {
        'categories': ['Banks', 'Insurance', 'Asset_Management', 'Financial_Services'],
        'description': 'Distinct sub-industries with different drivers (NIM, underwriting, AUM, credit)'
    },
    'VIS': {
        'categories': ['Capital_Goods_Aerospace', 'Transportation', 'Commercial_Services'],
        'description': 'Capital goods/defense are capex-driven, transport is volume-driven, services are labor-driven'
    },
    'VCR': {
        'categories': ['Retail_Services', 'Automotive_Durables'],
        'description': 'Consumer services/retail are discretionary spending, auto/durables are big-ticket cyclical'
    }
}

# =============================================================================
# INDUSTRY CLASSIFICATION MAPPING
# =============================================================================

INDUSTRY_CATEGORY_MAP = {
    # FINANCIALS (VFH)
    'Banks': [
        'Banks—Diversified', 'Banks—Regional', 'Banks - Diversified',
        'Banks - Regional', 'Diversified Banks', 'Regional Banks',
        'Commercial Banks', 'Money Center Banks', 'Savings & Loans',
        'Banks—Regional—US', 'Banks—Regional—Latin America',
    ],
    'Insurance': [
        'Insurance—Life', 'Insurance—Property & Casualty',
        'Insurance—Diversified', 'Insurance—Specialty',
        'Insurance - Life', 'Insurance - Property & Casualty',
        'Insurance—Reinsurance', 'Insurance Brokers',
        'Life Insurance', 'Property & Casualty Insurance',
        'Multi-line Insurance', 'Reinsurance', 'Insurance—Brokers',
    ],
    'Asset_Management': [
        'Asset Management', 'Capital Markets', 'Investment Banking & Brokerage',
        'Financial Data & Stock Exchanges', 'Investment Brokerage - National',
        'Investment Brokerage - Regional', 'Investment Management',
        'Private Equity', 'Financial Exchanges & Data',
    ],
    'Financial_Services': [
        'Credit Services', 'Consumer Finance', 'Specialty Finance',
        'Mortgage Finance', 'Financial Conglomerates', 'Financial Services',
        'Credit Card', 'Payment Services',
        'Financial Administration', 'Diversified Financial Services',
    ],
    'mREIT': [
        'Mortgage REIT', 'REIT - Mortgage',
    ],

    # TECHNOLOGY (VGT + VOX)
    'Software_Services': [
        'Software—Application', 'Software—Infrastructure',
        'Software - Application', 'Software - Infrastructure',
        'Information Technology Services', 'IT Services',
        'Internet Content & Information', 'Internet Software/Services',
        'Data Processing & Outsourced Services', 'Application Software',
        'Systems Software', 'Internet Services & Infrastructure',
        'Electronic Data Processing', 'Computer Services',
    ],
    'Hardware_Equipment': [
        'Computer Hardware', 'Electronic Components',
        'Consumer Electronics', 'Electronic Equipment & Instruments',
        'Technology Hardware, Storage & Peripherals', 'Communications Equipment',
        'Scientific & Technical Instruments', 'Networking Equipment',
        'Computer Peripherals', 'Office Equipment',
    ],
    'Semiconductors_AI': [
        'Semiconductors', 'Semiconductor Equipment & Materials',
        'Semiconductor - Broad Line', 'Semiconductor - Memory Chips',
        'Semiconductor - Analog', 'Semiconductor - Integrated Circuits',
        'Semiconductor Equipment', 'Semiconductor Materials',
    ],
    'Communication_Services': [
        'Telecom Services', 'Telecommunication Services',
        'Integrated Telecommunication Services', 'Wireless Telecommunication Services',
        'Alternative Carriers', 'Broadcasting', 'Cable & Satellite',
        'Entertainment', 'Interactive Media & Services', 'Media',
        'Advertising Agencies', 'Publishing', 'Movies & Entertainment',
    ],

    # INDUSTRIALS (VIS)
    'Capital_Goods': [
        'Machinery', 'Industrial Machinery', 'Farm & Heavy Construction Machinery',
        'Electrical Components & Equipment', 'Industrial Conglomerates',
        'Construction Machinery & Heavy Trucks', 'Building Products',
        'Heavy Electrical Equipment', 'Industrial Equipment',
        'Specialty Industrial Machinery', 'Metal Fabrication',
    ],
    'Aerospace_Defense': [
        'Aerospace & Defense', 'Defense', 'Aerospace',
        'Defense Primes', 'Aerospace/Defense Products & Services',
    ],
    'Transportation': [
        'Airlines', 'Air Freight & Logistics', 'Trucking',
        'Railroads', 'Marine', 'Marine Shipping', 'Integrated Freight & Logistics',
        'Airport Services', 'Highways & Railtracks', 'Transportation Infrastructure',
    ],
    'Commercial_Services': [
        'Professional Services', 'Commercial Services & Supplies',
        'Research & Consulting Services', 'Human Resource & Employment Services',
        'Environmental & Facilities Services', 'Security & Protection Services',
        'Staffing & Employment Services', 'Diversified Support Services',
        'Waste Management', 'Business Services',
    ],

    # HEALTHCARE (VHT)
    'Biotechnology': [
        'Biotechnology',
    ],
    'Drug_Manufacturers_General': [
        'Drug Manufacturers—General', 'Drug Manufacturers - General',
        'Drug Manufacturers - Major',
    ],
    'Drug_Manufacturers_Specialty': [
        'Drug Manufacturers—Specialty & Generic',
        'Drug Manufacturers - Specialty & Generic',
        'Pharmaceuticals', 'Biopharmaceuticals',
    ],
    'Medical_Devices': [
        'Medical Devices', 'Health Care Equipment', 'Medical Instruments & Supplies',
        'Medical Equipment', 'Health Care Supplies', 'Medical Distribution',
    ],
    'Healthcare_Services': [
        'Health Care Providers & Services', 'Medical Care Facilities',
        'Managed Health Care', 'Health Care Facilities', 'Healthcare Plans',
        'Health Care Services', 'Hospitals', 'Health Information Services',
        'Health Care Distributors', 'Health Maintenance Organizations',
    ],
    'Life_Sciences': [
        'Life Sciences Tools & Services', 'Diagnostics & Research',
        'Health Care Technology', 'Research Services',
    ],

    # CONSUMER DISCRETIONARY (VCR)
    'Retail': [
        'Internet Retail', 'Specialty Retail', 'Apparel Retail',
        'Home Improvement Retail', 'Automotive Retail', 'Department Stores',
        'General Merchandise Stores', 'Broadline Retail', 'Discount Stores',
        'Computer & Electronics Retail', 'Home Furnishing Retail',
    ],
    'Automotive': [
        'Auto Manufacturers', 'Auto Parts', 'Automobiles',
        'Auto & Truck Dealerships', 'Automobile Manufacturers',
        'Automotive Parts & Equipment', 'Tires & Rubber',
    ],
    'Consumer_Services': [
        'Restaurants', 'Hotels, Resorts & Cruise Lines', 'Leisure',
        'Casinos & Gaming', 'Hotels & Motels', 'Resorts & Casinos',
        'Leisure Products', 'Leisure Facilities', 'Travel Services',
        'Education Services', 'Personal Services',
    ],
    'Consumer_Durables': [
        'Household Durables', 'Homebuilding', 'Household Appliances',
        'Housewares & Specialties', 'Home Furnishings', 'Footwear',
        'Apparel, Accessories & Luxury Goods', 'Textiles, Apparel & Luxury Goods',
        'Textile Manufacturing', 'Furniture',
    ],
}

# Create reverse lookup
INDUSTRY_TO_CATEGORY = {}
for category, industries in INDUSTRY_CATEGORY_MAP.items():
    for industry in industries:
        INDUSTRY_TO_CATEGORY[industry.lower()] = category

# =============================================================================
# CONSOLIDATION MAPPING
# =============================================================================

CONSOLIDATION_MAP = {
    # VGT: 2 categories
    ('VGT', 'Software_Services'): 'Tech_Core',
    ('VGT', 'Hardware_Equipment'): 'Tech_Core',
    ('VGT', 'Communication_Services'): 'Tech_Core',
    ('VGT', 'Semiconductors_AI'): 'Semiconductors_AI',
    ('VGT', 'Unclassified'): 'Tech_Core',

    # VHT: 3 categories
    ('VHT', 'Biotechnology'): 'Biotech',
    ('VHT', 'Drug_Manufacturers_General'): 'Large_Pharma_Services',
    ('VHT', 'Drug_Manufacturers_Specialty'): 'Large_Pharma_Services',
    ('VHT', 'Healthcare_Services'): 'Large_Pharma_Services',
    ('VHT', 'Medical_Devices'): 'Medical_Devices_LifeSci',
    ('VHT', 'Life_Sciences'): 'Medical_Devices_LifeSci',
    ('VHT', 'Unclassified'): 'Medical_Devices_LifeSci',

    # VFH: 4 categories (mREIT excluded)
    ('VFH', 'Banks'): 'Banks',
    ('VFH', 'Insurance'): 'Insurance',
    ('VFH', 'Asset_Management'): 'Asset_Management',
    ('VFH', 'Financial_Services'): 'Financial_Services',
    ('VFH', 'Software_Services'): 'Financial_Services',
    ('VFH', 'Unclassified'): 'Financial_Services',
    ('VFH', 'mREIT'): 'EXCLUDE',

    # VIS: 3 categories
    ('VIS', 'Capital_Goods'): 'Capital_Goods_Aerospace',
    ('VIS', 'Aerospace_Defense'): 'Capital_Goods_Aerospace',
    ('VIS', 'Transportation'): 'Transportation',
    ('VIS', 'Automotive'): 'Transportation',
    ('VIS', 'Commercial_Services'): 'Commercial_Services',
    ('VIS', 'Software_Services'): 'Commercial_Services',
    ('VIS', 'Unclassified'): 'Commercial_Services',

    # VCR: 2 categories
    ('VCR', 'Consumer_Services'): 'Retail_Services',
    ('VCR', 'Retail'): 'Retail_Services',
    ('VCR', 'Automotive'): 'Automotive_Durables',
    ('VCR', 'Consumer_Durables'): 'Automotive_Durables',
    ('VCR', 'Hardware_Equipment'): 'Automotive_Durables',
    ('VCR', 'Unclassified'): 'Retail_Services',
}

ETF_DEFAULT_CATEGORY = {
    'VGT': 'Tech_Core',
    'VHT': 'Medical_Devices_LifeSci',
    'VFH': 'Financial_Services',
    'VIS': 'Commercial_Services',
    'VCR': 'Retail_Services',
}

# Megacap detection configuration
MEGACAP_CONFIG = {
    'method': 'statistical',
    'statistical_multiplier': 2.5,
    'threshold_multiplier': 4.5,
    'min_weight_threshold': 0.03,
    'min_absolute_mcap': 300e9,
}

# Cache for market cap data to avoid duplicate API calls
market_cap_cache = {}

# =============================================================================
# CLASSIFICATION FUNCTIONS
# =============================================================================

def classify_industry_granular(industry_str, sector_str=None):
    """
    Map an industry string to a GRANULAR category.
    Returns (granular_category, is_mreit)
    """
    if not industry_str or industry_str == 'N/A':
        return ('Unclassified', False)

    industry_lower = industry_str.lower().strip()

    # Check for mREIT
    is_mreit = 'mortgage' in industry_lower and 'reit' in industry_lower

    # Direct lookup
    if industry_lower in INDUSTRY_TO_CATEGORY:
        category = INDUSTRY_TO_CATEGORY[industry_lower]
        is_mreit = category == 'mREIT'
        return (category, is_mreit)

    # Fuzzy matching
    for mapped_industry, category in INDUSTRY_TO_CATEGORY.items():
        if mapped_industry in industry_lower or industry_lower in mapped_industry:
            is_mreit = category == 'mREIT'
            return (category, is_mreit)

    # Fallback based on sector
    if sector_str:
        sector_lower = sector_str.lower()
        if 'technology' in sector_lower:
            return ('Software_Services', False)
        elif 'financial' in sector_lower:
            return ('Financial_Services', False)
        elif 'health' in sector_lower:
            return ('Healthcare_Services', False)
        elif 'industrial' in sector_lower:
            return ('Commercial_Services', False)
        elif 'consumer' in sector_lower:
            return ('Consumer_Services', False)

    return ('Unclassified', False)


def consolidate_category(etf, granular_category, industry_str=None):
    """
    Map granular category to consolidated category for a specific ETF.
    """
    # Special handling for VHT biotechnology vs pharma
    if etf == 'VHT' and industry_str:
        industry_lower = industry_str.lower()
        if 'biotechnology' in industry_lower:
            return 'Biotech'
        elif 'drug manufacturers' in industry_lower:
            return 'Large_Pharma_Services'

    # Look up in consolidation map
    key = (etf, granular_category)
    if key in CONSOLIDATION_MAP:
        return CONSOLIDATION_MAP[key]

    # Use default for ETF
    return ETF_DEFAULT_CATEGORY.get(etf, 'Unclassified')


def reconsolidate_after_integration(holdings_df, target_etf):
    """
    Re-apply consolidation logic after VOX holdings are integrated.
    """
    def apply_consolidation(row):
        granular = row.get('Granular_Category', 'Unclassified')
        industry = row.get('Industry', 'N/A')

        # Special handling for VHT
        if target_etf == 'VHT' and pd.notna(industry):
            industry_lower = str(industry).lower()
            if 'biotechnology' in industry_lower:
                return 'Biotech'
            elif 'drug manufacturers' in industry_lower:
                return 'Large_Pharma_Services'

        key = (target_etf, granular)
        if key in CONSOLIDATION_MAP:
            result = CONSOLIDATION_MAP[key]
            if result != 'EXCLUDE':
                return result

        return ETF_DEFAULT_CATEGORY.get(target_etf, 'Unclassified')

    holdings_df['Category'] = holdings_df.apply(apply_consolidation, axis=1)
    return holdings_df

# =============================================================================
# API FUNCTIONS
# =============================================================================

def get_etf_holdings_alphavantage(ticker_symbol, api_key=API_KEY):
    """
    Retrieve ETF profile and holdings using Alpha Vantage API
    """
    print(f"  Fetching ETF data for {ticker_symbol}...")

    url = f'https://www.alphavantage.co/query?function=ETF_PROFILE&symbol={ticker_symbol}&apikey={api_key}&outputsize=full'

    try:
        response = requests.get(url, timeout=10)

        if response.status_code == 200:
            data = response.json()

            if "Note" in data or "Information" in data:
                print(f"  API limit reached: {data.get('Note', data.get('Information'))}")
                return None, None

            profile_data = {
                'symbol': data.get('symbol', ticker_symbol),
                'name': data.get('name', 'N/A'),
                'expense_ratio': data.get('expense_ratio', 'N/A'),
                'total_assets': data.get('net_assets', 'N/A'),
                'dividend_yield': data.get('dividend_yield', 'N/A'),
                'num_holdings': data.get('holdings_count', 'N/A')
            }

            holdings_list = []
            if 'holdings' in data:
                for holding in data['holdings']:
                    weight = float(holding.get('weight', 0))
                    weight_pct = weight * 100

                    holdings_list.append({
                        'Security': holding.get('description', 'N/A'),
                        'Ticker': holding.get('symbol', 'N/A'),
                        'Weight %': weight_pct,
                        'Shares': holding.get('shares', 'N/A'),
                        'Original_ETF': ticker_symbol
                    })

            holdings_df = pd.DataFrame(holdings_list)

            if not holdings_df.empty:
                holdings_df = holdings_df.sort_values('Weight %', ascending=False)

            print(f"  Successfully retrieved {len(holdings_df)} holdings for {ticker_symbol}")
            return profile_data, holdings_df

        else:
            print(f"  API request failed with status code: {response.status_code}")
            return None, None

    except Exception as e:
        print(f"  Error fetching data: {str(e)}")
        return None, None


# Track failed tickers for reporting
failed_tickers_log = []

def get_stock_overview(ticker, api_key=API_KEY, max_retries=3):
    """Get market cap and industry data for a single stock using cache with retry logic"""

    if ticker in market_cap_cache:
        return market_cap_cache[ticker]

    if ticker == 'N/A' or not ticker or str(ticker).strip() == '':
        return {'market_cap': 0, 'market_cap_formatted': 'N/A', 'sector': 'N/A', 'industry': 'N/A'}

    url = f'https://www.alphavantage.co/query?function=OVERVIEW&symbol={ticker}&apikey={api_key}'

    last_error = None
    for attempt in range(max_retries):
        try:
            response = requests.get(url, timeout=10)

            if response.status_code == 200:
                data = response.json()

                # Check for API rate limit message
                if "Note" in data or "Information" in data:
                    wait_time = (2 ** attempt) * 5 + random.uniform(1, 3)
                    print(f"    Rate limited on {ticker}, waiting {wait_time:.1f}s (attempt {attempt+1}/{max_retries})")
                    time.sleep(wait_time)
                    continue

                if 'MarketCapitalization' in data:
                    market_cap_str = data.get('MarketCapitalization', '0')
                    # Handle None or empty string
                    if market_cap_str is None or market_cap_str == 'None' or market_cap_str == '':
                        market_cap = 0
                    else:
                        market_cap = float(market_cap_str)

                    result = {
                        'market_cap': market_cap,
                        'market_cap_formatted': format_market_cap(market_cap),
                        'pe_ratio': data.get('PERatio', 'N/A'),
                        'sector': data.get('Sector', 'N/A'),
                        'industry': data.get('Industry', 'N/A')
                    }
                    market_cap_cache[ticker] = result
                    return result
                else:
                    # API returned but no market cap data (possibly delisted/invalid ticker)
                    # Don't retry, just return empty
                    break

            elif response.status_code == 429:  # Too Many Requests
                wait_time = (2 ** attempt) * 10 + random.uniform(1, 5)
                print(f"    HTTP 429 on {ticker}, waiting {wait_time:.1f}s (attempt {attempt+1}/{max_retries})")
                time.sleep(wait_time)
                continue
            else:
                last_error = f"HTTP {response.status_code}"

        except requests.exceptions.Timeout:
            last_error = "Timeout"
            wait_time = (2 ** attempt) * 2 + random.uniform(0.5, 2)
            if attempt < max_retries - 1:
                print(f"    Timeout on {ticker}, retrying in {wait_time:.1f}s (attempt {attempt+1}/{max_retries})")
                time.sleep(wait_time)

        except requests.exceptions.ConnectionError as e:
            last_error = "Connection error"
            wait_time = (2 ** attempt) * 3 + random.uniform(1, 3)
            if attempt < max_retries - 1:
                print(f"    Connection error on {ticker}, retrying in {wait_time:.1f}s (attempt {attempt+1}/{max_retries})")
                time.sleep(wait_time)

        except ValueError as e:
            # Handle float conversion errors
            last_error = f"Value error: {e}"
            break  # Don't retry on value errors

        except Exception as e:
            last_error = str(e)
            wait_time = (2 ** attempt) * 2 + random.uniform(0.5, 2)
            if attempt < max_retries - 1:
                print(f"    Error on {ticker}: {last_error}, retrying in {wait_time:.1f}s")
                time.sleep(wait_time)

    # All retries failed
    if last_error:
        failed_tickers_log.append({'ticker': ticker, 'error': last_error})
        print(f"    Failed to get data for {ticker} after {max_retries} attempts: {last_error}")

    result = {'market_cap': 0, 'market_cap_formatted': 'N/A', 'sector': 'N/A', 'industry': 'N/A'}
    market_cap_cache[ticker] = result
    return result


def format_market_cap(market_cap):
    """Format market cap in billions or trillions"""
    if market_cap == 0 or market_cap == 'N/A':
        return 'N/A'

    if market_cap >= 1_000_000_000_000:
        return f"${market_cap / 1_000_000_000_000:.2f}T"
    elif market_cap >= 1_000_000_000:
        return f"${market_cap / 1_000_000_000:.1f}B"
    else:
        return f"${market_cap / 1_000_000:.1f}M"

# =============================================================================
# VOX INTEGRATION FUNCTIONS
# =============================================================================

def calculate_combined_index_weights(primary_holdings, secondary_holdings, primary_etf, secondary_etf, method='market_cap'):
    """
    Calculate proper weights for combined index (e.g., VGT + VOX)
    """
    print(f"\n  Calculating combined index weights for {primary_etf} + {secondary_etf}...")
    print(f"  Weight calculation method: {method}")

    all_holdings = []

    # Add primary ETF holdings
    for _, row in primary_holdings.iterrows():
        ticker = row['Ticker']
        if (pd.isna(ticker) or
            str(ticker).strip().lower() in ['n/a', 'na', '', 'nan', 'none'] or
            str(ticker).strip() == ''):
            continue

        holding = {
            'Ticker': ticker,
            'Security': row['Security'],
            'Original_Weight_%': row['Weight %'],
            'Original_ETF': primary_etf,
            'Mcap': row.get('Mcap', 0),
            'Market Cap': row.get('Market Cap', 'N/A'),
            'Sector': row.get('Sector', 'N/A'),
            'Industry': row.get('Industry', 'N/A'),
            'Granular_Category': row.get('Granular_Category', 'Unclassified'),
            'Category': row.get('Category', 'Unclassified'),
            'Shares': row.get('Shares', 'N/A')
        }
        all_holdings.append(holding)

    # Add secondary ETF holdings
    for _, row in secondary_holdings.iterrows():
        existing_ticker = next((h for h in all_holdings if h['Ticker'] == row['Ticker']), None)

        if existing_ticker:
            print(f"    Overlapping ticker found: {row['Ticker']}")
            existing_ticker['Original_ETF'] = f"{primary_etf}+{secondary_etf}"
            existing_ticker['Secondary_Weight_%'] = row['Weight %']
        else:
            holding = {
                'Ticker': row['Ticker'],
                'Security': row['Security'],
                'Original_Weight_%': 0,
                'Secondary_Weight_%': row['Weight %'],
                'Original_ETF': secondary_etf,
                'Mcap': row.get('Mcap', 0),
                'Market Cap': row.get('Market Cap', 'N/A'),
                'Sector': row.get('Sector', 'N/A'),
                'Industry': row.get('Industry', 'N/A'),
                'Granular_Category': row.get('Granular_Category', 'Unclassified'),
                'Category': row.get('Category', 'Unclassified'),
                'Shares': row.get('Shares', 'N/A')
            }
            all_holdings.append(holding)

    # Calculate new weights
    if method == 'market_cap':
        total_market_cap = sum(h['Mcap'] for h in all_holdings if h['Mcap'] > 0)

        if total_market_cap == 0:
            print(f"    Warning: No market cap data, using equal weights")
            method = 'equal_weight'
        else:
            print(f"    Total combined market cap: {format_market_cap(total_market_cap)}")
            for holding in all_holdings:
                if holding['Mcap'] > 0:
                    holding['New_Weight_%'] = (holding['Mcap'] / total_market_cap) * 100
                else:
                    holding['New_Weight_%'] = 0

    if method == 'equal_weight':
        equal_weight = 100.0 / len(all_holdings)
        for holding in all_holdings:
            holding['New_Weight_%'] = equal_weight

    combined_df = pd.DataFrame(all_holdings)
    combined_df['Weight %'] = combined_df['New_Weight_%']

    column_order = ['Security', 'Ticker', 'Weight %', 'Mcap', 'Market Cap', 'Sector', 'Industry',
                    'Granular_Category', 'Category', 'Shares', 'Original_ETF']
    for col in column_order:
        if col not in combined_df.columns:
            combined_df[col] = 'N/A'

    final_df = combined_df[column_order].copy()
    final_df = final_df.sort_values('Weight %', ascending=False)

    total_weight = final_df['Weight %'].sum()
    print(f"    Combined index: {len(final_df)} holdings, weight sum: {total_weight:.2f}%")

    return final_df


def integrate_vox_holdings(vgt_holdings, vox_holdings):
    """
    Integrate VOX holdings into VGT holdings with proper weight recalculation
    """
    print(f"\n  Integrating VOX holdings into VGT...")

    if vox_holdings is None or vox_holdings.empty:
        print(f"  No VOX holdings to integrate")
        return vgt_holdings

    vox_config = VOX_INTEGRATION.get('VGT', {})
    if not vox_config.get('recalculate_weights', False):
        # Simple integration without weight recalculation
        vgt_tickers = set(vgt_holdings['Ticker'].tolist())
        vox_new = vox_holdings[~vox_holdings['Ticker'].isin(vgt_tickers)].copy()

        if vox_new.empty:
            return vgt_holdings

        vox_new['Weight %'] = 0.01
        combined = pd.concat([vgt_holdings, vox_new], ignore_index=True)
        return combined.sort_values('Weight %', ascending=False)

    weight_method = vox_config.get('weight_method', 'market_cap')
    combined_holdings = calculate_combined_index_weights(
        vgt_holdings, vox_holdings, 'VGT', 'VOX', weight_method
    )

    return combined_holdings

# =============================================================================
# ENRICHMENT FUNCTION
# =============================================================================

def enrich_holdings_with_market_caps(holdings_df, etf_ticker):
    """
    Add market cap, industry, and category data to all holdings.
    Applies exclusions for crypto, mREITs, and failed API lookups.
    """
    print(f"\n  Enriching {len(holdings_df)} holdings for {etf_ticker}...")

    # Initialize new columns
    holdings_df['Market Cap'] = 'N/A'
    holdings_df['Mcap'] = 0
    holdings_df['Sector'] = 'N/A'
    holdings_df['Industry'] = 'N/A'
    holdings_df['Granular_Category'] = 'Unclassified'
    holdings_df['Category'] = 'Unclassified'

    # Track exclusions
    crypto_excluded = []
    mreit_excluded = []
    api_failed = []
    rows_to_keep = []

    total_holdings = len(holdings_df)

    for idx, row in holdings_df.iterrows():
        ticker = row['Ticker']

        # Skip invalid tickers
        if pd.isna(ticker) or str(ticker).strip().lower() in ['n/a', 'na', '', 'nan', 'none']:
            continue

        # Check crypto exclusion
        if ticker in CRYPTO_TICKERS:
            crypto_excluded.append(ticker)
            continue

        # Get stock data (with retry logic)
        stock_data = get_stock_overview(ticker)

        industry_str = stock_data.get('industry', 'N/A')
        sector_str = stock_data.get('sector', 'N/A')
        market_cap = stock_data.get('market_cap', 0)

        # Check if API call failed (no valid data returned)
        if market_cap == 0 and industry_str == 'N/A' and sector_str == 'N/A':
            api_failed.append(ticker)
            continue  # Exclude tickers with no valid data

        # Classify industry
        granular_category, is_mreit = classify_industry_granular(industry_str, sector_str)

        # Check mREIT exclusion
        if MREIT_EXCLUSION and is_mreit:
            mreit_excluded.append(ticker)
            continue

        # Get consolidated category
        consolidated_category = consolidate_category(etf_ticker, granular_category, industry_str)

        # Skip if consolidation returns EXCLUDE
        if consolidated_category == 'EXCLUDE':
            mreit_excluded.append(ticker)
            continue

        # Update row data
        holdings_df.at[idx, 'Market Cap'] = stock_data['market_cap_formatted']
        holdings_df.at[idx, 'Mcap'] = stock_data['market_cap']
        holdings_df.at[idx, 'Sector'] = sector_str
        holdings_df.at[idx, 'Industry'] = industry_str
        holdings_df.at[idx, 'Granular_Category'] = granular_category
        holdings_df.at[idx, 'Category'] = consolidated_category

        rows_to_keep.append(idx)

        # Progress indicator
        processed = len(rows_to_keep) + len(crypto_excluded) + len(mreit_excluded) + len(api_failed)
        if processed % 25 == 0:
            print(f"    Progress: {processed}/{total_holdings}...")

        time.sleep(0.15)  # Slightly longer delay to be gentler on API

    # Filter to only kept rows
    enriched_df = holdings_df.loc[rows_to_keep].copy()
    enriched_df = enriched_df.sort_values('Mcap', ascending=False)

    # Report exclusions
    print(f"  Enrichment complete:")
    print(f"    - Tickers included: {len(enriched_df)}")
    if crypto_excluded:
        print(f"    - Crypto excluded: {len(crypto_excluded)} ({', '.join(crypto_excluded)})")
    if mreit_excluded:
        print(f"    - mREITs excluded: {len(mreit_excluded)} ({', '.join(mreit_excluded)})")
    if api_failed:
        print(f"    - API failed (excluded): {len(api_failed)} ({', '.join(api_failed[:10])}{'...' if len(api_failed) > 10 else ''})")

    # Show category breakdown
    if len(enriched_df) > 0:
        print(f"    - Category breakdown:")
        for cat, count in enriched_df['Category'].value_counts().items():
            print(f"        {cat}: {count}")

    return enriched_df

# =============================================================================
# MEGACAP DETECTION
# =============================================================================

def detect_megacaps(holdings_df, etf_ticker):
    """
    Automatically detect megacap stocks based on market cap and weight
    """
    print(f"\n  Detecting megacaps for {etf_ticker}...")

    if holdings_df is None or holdings_df.empty:
        return []

    valid_holdings = holdings_df[
        (holdings_df['Mcap'] > 0) &
        (holdings_df['Weight %'] > 0)
    ].copy()

    if valid_holdings.empty:
        print(f"  No valid holdings with market cap data")
        return []

    megacaps = []
    method = MEGACAP_CONFIG['method']

    if method == 'statistical':
        mcaps = valid_holdings['Mcap'].values
        mcap_mean = np.mean(mcaps)
        mcap_std = np.std(mcaps)
        mcap_threshold = mcap_mean + (MEGACAP_CONFIG['statistical_multiplier'] * mcap_std)

        print(f"    Threshold ({MEGACAP_CONFIG['statistical_multiplier']}sigma): {format_market_cap(mcap_threshold)}")

        megacap_candidates = valid_holdings[
            (valid_holdings['Mcap'] >= mcap_threshold) &
            (valid_holdings['Weight %'] >= MEGACAP_CONFIG['min_weight_threshold'] * 100) &
            (valid_holdings['Mcap'] >= MEGACAP_CONFIG['min_absolute_mcap'])
        ]
    else:
        mcap_median = np.median(valid_holdings['Mcap'])
        mcap_threshold = mcap_median * MEGACAP_CONFIG['threshold_multiplier']

        megacap_candidates = valid_holdings[
            (valid_holdings['Mcap'] >= mcap_threshold) &
            (valid_holdings['Weight %'] >= MEGACAP_CONFIG['min_weight_threshold'] * 100) &
            (valid_holdings['Mcap'] >= MEGACAP_CONFIG['min_absolute_mcap'])
        ]

    if not megacap_candidates.empty:
        for _, row in megacap_candidates.iterrows():
            megacap_info = {
                'ETF': etf_ticker,
                'Ticker': row['Ticker'],
                'Security': row['Security'],
                '% of fund': row['Weight %'] / 100,
                'Mcap': row['Mcap'],
                'Market Cap': row['Market Cap'],
                'Category': row.get('Category', 'N/A'),
                'Original_ETF': row.get('Original_ETF', etf_ticker)
            }
            megacaps.append(megacap_info)

        print(f"    Detected {len(megacaps)} megacaps:")
        for mc in megacaps:
            print(f"      {mc['Ticker']}: {mc['Market Cap']} ({mc['% of fund']:.1%})")
    else:
        print(f"    No megacaps detected")

    return megacaps

# =============================================================================
# EXCEL OUTPUT
# =============================================================================

def save_etf_data_to_excel(etf_data_dict, all_megacaps, output_path, filename):
    """Save ETF data to Excel with each ETF on a separate tab"""

    os.makedirs(output_path, exist_ok=True)
    full_path = os.path.join(output_path, filename)

    print(f"\nSaving data to: {full_path}")

    try:
        with pd.ExcelWriter(full_path, engine='openpyxl') as writer:

            summary_data = []

            # Create ETF sheets
            for etf_ticker, data in etf_data_dict.items():
                profile = data['profile']
                holdings = data['holdings']

                if holdings is not None and not holdings.empty:
                    # Column order for Excel output
                    excel_cols = ['Security', 'Ticker', 'Weight %', 'Mcap', 'Market Cap',
                                  'Category', 'Granular_Category', 'Sector', 'Industry',
                                  'Shares', 'Original_ETF']
                    excel_cols = [c for c in excel_cols if c in holdings.columns]
                    excel_df = holdings[excel_cols].copy()

                    # Format weight percentage
                    weight_formatted = excel_df['Weight %'].apply(lambda x: f"{x:.2f}%")

                    excel_df.to_excel(writer, sheet_name=etf_ticker, index=False)
                    print(f"  Created ETF sheet: {etf_ticker}")

                    worksheet = writer.sheets[etf_ticker]

                    for row_num, value in enumerate(weight_formatted, start=2):
                        worksheet.cell(row=row_num, column=3).value = value

                    # Column widths
                    worksheet.column_dimensions['A'].width = 40
                    worksheet.column_dimensions['B'].width = 10
                    worksheet.column_dimensions['C'].width = 10
                    worksheet.column_dimensions['D'].width = 15
                    worksheet.column_dimensions['E'].width = 15
                    worksheet.column_dimensions['F'].width = 25
                    worksheet.column_dimensions['G'].width = 20
                    worksheet.column_dimensions['H'].width = 20
                    worksheet.column_dimensions['I'].width = 25

                    # Add header info
                    worksheet.insert_rows(1, 5)
                    worksheet['A1'] = f"ETF: {etf_ticker}"
                    worksheet['A2'] = f"Name: {profile.get('name', 'N/A')}"
                    worksheet['A3'] = f"Total Holdings: {len(holdings)}"
                    worksheet['A4'] = f"Data Retrieved: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
                    worksheet['A5'] = f"Categories: {', '.join(CONSOLIDATED_CATEGORIES[etf_ticker]['categories'])}"

                    # Summary data
                    top_10_weight = holdings.head(10)['Weight %'].sum()
                    summary_data.append({
                        'ETF': etf_ticker,
                        'Name': profile.get('name', 'N/A'),
                        'Holdings Count': len(holdings),
                        'Top 10 Weight': f"{top_10_weight:.1f}%",
                        'Categories': len(CONSOLIDATED_CATEGORIES[etf_ticker]['categories']),
                        'Expense Ratio': profile.get('expense_ratio', 'N/A'),
                    })

            # Create Megacaps sheet
            if all_megacaps:
                megacaps_df = pd.DataFrame(all_megacaps)
                column_order = ['ETF', 'Ticker', 'Security', '% of fund', 'Mcap', 'Market Cap', 'Category', 'Original_ETF']
                megacaps_df = megacaps_df[[c for c in column_order if c in megacaps_df.columns]]
                megacaps_df = megacaps_df.sort_values(['ETF', 'Mcap'], ascending=[True, False])

                megacaps_df.to_excel(writer, sheet_name='Megacaps', index=False)
                print(f"  Created Megacaps sheet with {len(megacaps_df)} entries")

                worksheet = writer.sheets['Megacaps']
                worksheet.insert_rows(1, 4)
                worksheet['A1'] = f"Automated Megacap Detection Results"
                worksheet['A2'] = f"Detection Method: {MEGACAP_CONFIG['method']}"
                worksheet['A3'] = f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

            # Create Category Structure sheet
            structure_data = []
            for etf, etf_config in CONSOLIDATED_CATEGORIES.items():
                if etf in etf_data_dict:
                    holdings = etf_data_dict[etf]['holdings']
                    for cat in etf_config['categories']:
                        count = (holdings['Category'] == cat).sum() if len(holdings) > 0 else 0
                        structure_data.append({
                            'ETF': etf,
                            'Category': cat,
                            'Ticker_Count': count,
                            'Description': etf_config['description']
                        })

            if structure_data:
                structure_df = pd.DataFrame(structure_data)
                structure_df.to_excel(writer, sheet_name='Category_Structure', index=False)
                print(f"  Created Category_Structure sheet")

            # Create Summary sheet
            if summary_data:
                summary_df = pd.DataFrame(summary_data)
                summary_df.to_excel(writer, sheet_name='Summary', index=False)
                print(f"  Created Summary sheet")

        print(f"\nSuccessfully saved: {filename}")
        return full_path

    except Exception as e:
        print(f"Error saving Excel file: {str(e)}")
        import traceback
        traceback.print_exc()
        return None

# =============================================================================
# MAIN EXECUTION
# =============================================================================

def main():
    """Main function to retrieve and save ETF data"""

    print(f"{'='*80}")
    print(f"UNIVERSE DETERMINATION - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*80}")
    print(f"ETFs to analyze: {', '.join(ETF_TICKERS)}")
    print(f"Crypto exclusions: {len(CRYPTO_TICKERS)} tickers")
    print(f"mREIT exclusion: {MREIT_EXCLUSION}")
    print(f"Output path: {OUTPUT_PATH}")
    print(f"Output file: {OUTPUT_FILENAME}")
    print(f"\nConsolidated category structure:")
    for etf, etf_config in CONSOLIDATED_CATEGORIES.items():
        print(f"  {etf}: {', '.join(etf_config['categories'])}")
    print(f"{'='*80}\n")

    etf_data = {}
    all_megacaps = []

    # Step 1: Get VOX data if needed
    vox_data = None
    if any(vox_cfg.get('integrate_etf') == 'VOX' for vox_cfg in VOX_INTEGRATION.values()):
        print(f"Fetching VOX data for integration...")
        vox_profile, vox_holdings = get_etf_holdings_alphavantage('VOX')
        if vox_profile and vox_holdings is not None:
            vox_holdings = enrich_holdings_with_market_caps(vox_holdings.copy(), 'VOX')
            vox_data = {'profile': vox_profile, 'holdings': vox_holdings}
            print(f"  VOX ready: {len(vox_holdings)} holdings")
        else:
            print(f"  Failed to retrieve VOX data")
        time.sleep(2)

    # Step 2: Process each ETF
    for i, etf_ticker in enumerate(ETF_TICKERS, 1):
        print(f"\n[{i}/{len(ETF_TICKERS)}] Processing {etf_ticker}...")
        print("-"*40)

        profile, holdings = get_etf_holdings_alphavantage(etf_ticker)

        if profile and holdings is not None:
            # Enrich with market cap and category data
            enriched_holdings = enrich_holdings_with_market_caps(holdings.copy(), etf_ticker)

            # Apply VOX integration if specified
            if etf_ticker in VOX_INTEGRATION and vox_data is not None:
                print(f"  Integrating VOX holdings...")
                original_count = len(enriched_holdings)
                enriched_holdings = integrate_vox_holdings(enriched_holdings, vox_data['holdings'])
                print(f"    Holdings: {original_count} -> {len(enriched_holdings)}")

                # Re-consolidate categories for target ETF
                enriched_holdings = reconsolidate_after_integration(enriched_holdings, etf_ticker)

                # Update profile
                profile['name'] = f"{profile.get('name', etf_ticker)} + VOX Combined"

            # Detect megacaps
            etf_megacaps = detect_megacaps(enriched_holdings, etf_ticker)
            all_megacaps.extend(etf_megacaps)

            # Store data
            etf_data[etf_ticker] = {
                'profile': profile,
                'holdings': enriched_holdings
            }

            print(f"  {etf_ticker}: {len(enriched_holdings)} holdings, {len(etf_megacaps)} megacaps")
        else:
            print(f"  Failed to retrieve data for {etf_ticker}")

        if i < len(ETF_TICKERS):
            time.sleep(2)

    # Step 3: Save to Excel
    if etf_data:
        output_file = save_etf_data_to_excel(etf_data, all_megacaps, OUTPUT_PATH, OUTPUT_FILENAME)

        print(f"\n{'='*80}")
        print(f"UNIVERSE DETERMINATION COMPLETE")
        print(f"{'='*80}")
        print(f"ETFs processed: {len(etf_data)}/{len(ETF_TICKERS)}")
        print(f"Total megacaps: {len(all_megacaps)}")

        # Category summary
        print(f"\nCategory counts per ETF:")
        for etf, data in etf_data.items():
            holdings = data['holdings']
            if len(holdings) == 0:
                continue
            print(f"\n  {etf} ({len(holdings)} tickers):")
            for cat in CONSOLIDATED_CATEGORIES[etf]['categories']:
                count = (holdings['Category'] == cat).sum()
                print(f"    {cat}: {count}")

        # Failed tickers summary
        if failed_tickers_log:
            print(f"\nAPI Failures Summary ({len(failed_tickers_log)} tickers):")
            # Group by error type
            error_counts = {}
            for entry in failed_tickers_log:
                error = entry['error']
                if 'Connection' in error or 'resolve' in error:
                    error_type = 'Network/Connection'
                elif 'Timeout' in error:
                    error_type = 'Timeout'
                elif 'Rate' in error or '429' in error:
                    error_type = 'Rate Limited'
                else:
                    error_type = 'Other'
                error_counts[error_type] = error_counts.get(error_type, 0) + 1

            for error_type, count in sorted(error_counts.items(), key=lambda x: -x[1]):
                print(f"    {error_type}: {count}")
            print(f"    (These tickers were excluded from the universe)")

        print(f"\nOutput: {OUTPUT_PATH}/{OUTPUT_FILENAME}")
        print(f"{'='*80}")
    else:
        print("\nNo data retrieved.")

if __name__ == "__main__":
    main()
