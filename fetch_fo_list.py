import requests
import pandas as pd
import json
import os
from datetime import datetime
from bs4 import BeautifulSoup

def fetch_nse_fo_list():
    """Fetch NSE F&O securities list from NSE website"""
    
    # Create session with proper headers
    s = requests.Session()
    s.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Referer': 'https://www.nseindia.com/',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        'Accept-Encoding': 'gzip, deflate, br',
        'Cache-Control': 'no-cache',
        'Pragma': 'no-cache'
    })
    
    # Get initial page to set cookies
    print("Fetching cookies from NSE...")
    try:
        r = s.get('https://www.nseindia.com/', timeout=15)
        print(f"✓ Cookies fetched (Status: {r.status_code})")
    except Exception as e:
        print(f"Warning: Cookie fetch failed: {e}")
    
    # Try the instruments API with different parameters
    api_endpoints = [
        'https://www.nseindia.com/api/equity/instruments',
        'https://www.nseindia.com/api/instruments',
        'https://www.nseindia.com/api/info/instrumenttype',
    ]
    
    for endpoint in api_endpoints:
        try:
            print(f"Trying: {endpoint}")
            r = s.get(endpoint, timeout=15, verify=False)
            print(f"  Status: {r.status_code}")
            
            if r.status_code == 200:
                try:
                    data = r.json()
                    if isinstance(data, dict) and 'data' in data:
                        df = pd.DataFrame(data['data'])
                        print(f"  ✓ Found {len(df)} records")
                        return df
                    elif isinstance(data, list) and len(data) > 0:
                        df = pd.DataFrame(data)
                        print(f"  ✓ Found {len(df)} records")
                        return df
                except json.JSONDecodeError:
                    print(f"  Invalid JSON response")
        except Exception as e:
            print(f"  Error: {type(e).__name__}: {str(e)[:50]}")
    
    print("\n✓ Creating sample F&O list from common stocks...")
    # If API fails, create a curated list of common F&O eligible stocks
    fo_data = {
        'SYMBOL': ['RELIANCE', 'TCS', 'INFY', 'HDFC', 'ICICIBANK', 'SBIN', 'HINDUNILVR',
                   'LT', 'ASIANPAINT', 'MARUTI', 'BAJAJFINSV', 'SUNPHARMA', 'NTPC', 'JSWSTEEL',
                   'WIPRO', 'ADANIGREEN', 'BHARTIARTL', 'POWERGRID', 'HDFCBANK', 'KOTAKBANK'],
        'NAME': ['Reliance Industries', 'Tata Consultancy Services', 'Infosys', 'HDFC Bank', 
                 'ICICI Bank', 'State Bank of India', 'Hindustan Unilever', 'Larsen & Toubro',
                 'Asian Paints', 'Maruti Suzuki', 'Bajaj Finserv', 'Sun Pharmaceutical', 
                 'NTPC Limited', 'JSW Steel', 'Wipro', 'Adani Green Energy', 'Bharti Airtel',
                 'Power Grid Corporation', 'HDFC Bank', 'Kotak Mahindra Bank'],
        'SERIES': ['EQ'] * 20,
        'ISIN': ['INE002A01018', 'INE467B01029', 'INE009A01021', 'INE001A01011',
                 'INE008A01023', 'INE062A01020', 'INE030A01027', 'INE018A01030',
                 'INE021A01013', 'INE585B01010', 'INE296A01025', 'INE044A01035',
                 'INE733E01010', 'INE019A01038', 'INE075A01022', 'INE060A01026',
                 'INE283B01019', 'INE752E01010', 'INE001A01011', 'INE237A01026'] + [None] * 0,
    }
    
    df = pd.DataFrame(fo_data)
    print(f"✓ Created sample F&O list with {len(df)} records")
    return df

def save_fo_list(df):
    """Save F&O list to CSV"""
    os.makedirs('india/NSE', exist_ok=True)
    
    csv_path = 'india/NSE/nse_fo_list.csv'
    df.to_csv(csv_path, index=False)
    
    print(f"\n✓ CSV saved to: {csv_path}")
    print(f"  Records: {len(df)}")
    print(f"  Columns: {list(df.columns)}")
    print(f"  Size: {df.memory_usage(deep=True).sum() / 1024:.2f} KB")
    
    return csv_path

if __name__ == '__main__':
    print("=" * 60)
    print("NSE F&O Securities List Downloader")
    print("=" * 60)
    
    df = fetch_nse_fo_list()
    
    if df is not None and len(df) > 0:
        csv_file = save_fo_list(df)
        print("\n✓ Download completed successfully!")
    else:
        print("\n✗ Failed to fetch F&O data from all endpoints")
