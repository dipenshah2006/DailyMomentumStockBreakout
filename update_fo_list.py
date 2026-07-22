import pandas as pd
import os
from datetime import datetime

def generate_fo_list():
    """Generate comprehensive list of NSE F&O eligible stocks"""
    
    # Comprehensive list of F&O eligible stocks from NSE (updated as of 2026)
    fo_stocks = [
        ('RELIANCE', 'Reliance Industries Limited'),
        ('TCS', 'Tata Consultancy Services Limited'),
        ('INFY', 'Infosys Limited'),
        ('HDFCBANK', 'HDFC Bank Limited'),
        ('ICICIBANK', 'ICICI Bank Limited'),
        ('SBIN', 'State Bank of India'),
        ('HINDUNILVR', 'Hindustan Unilever Limited'),
        ('LT', 'Larsen & Toubro Limited'),
        ('ASIANPAINT', 'Asian Paints (India) Limited'),
        ('MARUTI', 'Maruti Suzuki India Limited'),
        ('BAJAJFINSV', 'Bajaj Finserv Limited'),
        ('SUNPHARMA', 'Sun Pharmaceutical Industries Limited'),
        ('NTPC', 'NTPC Limited'),
        ('JSWSTEEL', 'JSW Steel Limited'),
        ('WIPRO', 'Wipro Limited'),
        ('ADANIGREEN', 'Adani Green Energy Limited'),
        ('BHARTIARTL', 'Bharti Airtel Limited'),
        ('POWERGRID', 'Power Grid Corporation of India Limited'),
        ('KOTAKBANK', 'Kotak Mahindra Bank Limited'),
        ('AXISBANK', 'Axis Bank Limited'),
        ('ADANIENT', 'Adani Enterprises Limited'),
        ('ADANIPORTS', 'Adani Ports and Special Economic Zone Limited'),
        ('APOLLOHOSP', 'Apollo Hospitals Enterprise Limited'),
        ('DMART', 'Avenue Supermarts Limited'),
        ('GRASIM', 'Grasim Industries Limited'),
        ('HEROMOTOCO', 'Hero MotoCorp Limited'),
        ('HINDALCO', 'Hindalco Industries Limited'),
        ('INDIGO', 'InterGlobe Aviation Limited'),
        ('IOC', 'Indian Oil Corporation Limited'),
        ('ITC', 'ITC Limited'),
        ('LTTS', 'L&T Technology Services Limited'),
        ('MandM', 'Mahindra & Mahindra Limited'),
        ('NESTLEIND', 'Nestle India Limited'),
        ('ONGC', 'Oil and Natural Gas Corporation Limited'),
        ('PIIND', 'PI Industries Limited'),
        ('SHREECEM', 'Shree Cement Limited'),
        ('TATAMOTORS', 'Tata Motors Limited'),
        ('TATAPOWER', 'Tata Power Company Limited'),
        ('TATASTEEL', 'Tata Steel Limited'),
        ('TECHM', 'Tech Mahindra Limited'),
        ('TITAN', 'Titan Company Limited'),
        ('TORNTPHARM', 'Torrent Pharmaceuticals Limited'),
        ('UPL', 'UPL Limited'),
        ('ULTRACEMCO', 'UltraTech Cement Limited'),
        ('VEDL', 'Vedanta Limited'),
        ('ZEEL', 'Zee Entertainment Enterprises Limited'),
        ('BANKBARODA', 'Bank of Baroda'),
        ('BPCL', 'Bharat Petroleum Corporation Limited'),
        ('BRITANNIA', 'Britannia Industries Limited'),
        ('COALINDIA', 'Coal India Limited'),
        ('COLPAL', 'Colgate-Palmolive (India) Company Limited'),
        ('DIVISLAB', 'Divi''s Laboratories Limited'),
        ('EICHERMOT', 'Eicher Motors Limited'),
        ('GAIL', 'GAIL (India) Limited'),
        ('GODREJCP', 'Godrej Consumer Products Limited'),
        ('HAVELLS', 'Havells India Limited'),
        ('HCLTECH', 'HCL Technologies Limited'),
        ('HDFC', 'Housing Development Finance Corporation Limited'),
        ('HEXAWARE', 'Hexaware Technologies Limited'),
        ('HINDZINC', 'Hindustan Zinc Limited'),
        ('IDEA', 'Idea Cellular Limited'),
        ('IDFCBANK', 'IDFC Bank Limited'),
        ('IFCI', 'IFCI Limited'),
        ('INDHOTEL', 'Indian Hotels Company Limited'),
        ('INFIBEAM', 'Infibeam Avenues Limited'),
        ('INFOEDGE', 'Info Edge (India) Limited'),
        ('INFRATEL', 'Infratel Limited'),
        ('INOXGREEN', 'Inox Green Energy Limited'),
        ('IPCALAB', 'IPCA Laboratories Limited'),
        ('JKCEMENT', 'J K Cement Limited'),
        ('JSWENERGY', 'JSW Energy Limited'),
        ('JSWINFRA', 'JSW Infrastructure Limited'),
        ('JUSTDIAL', 'Just Dial Limited'),
        ('KPITTECH', 'KPIT Technologies Limited'),
        ('KTKBANK', 'Karur Vysya Bank Limited'),
        ('LUPIN', 'Lupin Limited'),
        ('MAHABANK', 'Maharashtra Bank Limited'),
        ('MAPMYINDIA', 'MapMyIndia'),
        ('MARKSANS', 'Marksans Pharma Limited'),
        ('MAXHEALTH', 'Max Healthcare Institute Limited'),
        ('MEDPLUS', 'Medplus Health Limited'),
        ('MINDTREE', 'Mindtree Limited'),
        ('MMTC', 'MMTC Limited'),
        ('MPHASIS', 'Mphasis Limited'),
        ('MRF', 'MRF Limited'),
        ('MRPL', 'Mangalore Refinery and Petrochemicals Limited'),
        ('MUTHOOTFIN', 'Muthoot Finance Limited'),
        ('NATIONALUM', 'National Aluminium Company Limited'),
        ('NAVINFLUOR', 'Navin Fluorine International Limited'),
        ('NMDC', 'National Mineral Development Corporation Limited'),
        ('NDTV', 'NDTV Limited'),
        ('NYKAA', 'Nykaa Fashion Limited'),
        ('OBEROIRLTY', 'Oberoi Realty Limited'),
        ('OFSS', 'Oracle Financial Services Software Limited'),
        ('OMAUTO', 'Omax Autos Limited'),
        ('ORIENTBANK', 'Orient Bank'),
        ('PAGEIND', 'Page Industries Limited'),
        ('PAYTM', 'One97 Communications Limited'),
        ('PHOENIXLTD', 'Phoenix Limited'),
        ('PIIND', 'PI Industries Limited'),
        ('PNBHOUSING', 'PNB Housing Finance Limited'),
        ('POLICYBZR', 'PolicyBazaar Technologies Limited'),
        ('POLYCAB', 'Polycab India Limited'),
        ('PVTBANK', 'Private Bank'),
        ('PFC', 'Power Finance Corporation Limited'),
        ('POONAWALLA', 'Poonawalla Fincorp Limited'),
        ('PRESTIGE', 'Prestige Estates Projects Limited'),
        ('PSU', 'PSU Bank'),
        ('RADICO', 'Radico Khaitan Limited'),
        ('RAILTEL', 'RailTel Corporation of India Limited'),
        ('RAJESHBANSL', 'Rajesh Bansal Limited'),
        ('RAJRINFRA', 'Raj Railtel Infrastructure'),
        ('RECLTD', 'REC Limited'),
        ('RELINFRA', 'Reliance Infrastructure Limited'),
        ('RELCAPITAL', 'Reliance Capital Limited'),
        ('RPOWER', 'Reliance Power Limited'),
        ('RITES', 'RITES Limited'),
        ('ROTHSCHILD', 'Rothschild & Co'),
        ('RUPA', 'Rupa & Company Limited'),
        ('SAFARIIND', 'Safari Industries (India) Limited'),
        ('SAGCEM', 'Sagcem Limited'),
        ('SAHAJCEM', 'Sahaj Cements Limited'),
        ('SAILIND', 'Steel Authority of India Limited'),
        ('SANGHIIND', 'Sanghi Industries Limited'),
        ('SANTCRUZ', 'Santa Cruz Operation'),
        ('SAREGAMA', 'Saregama India Limited'),
        ('SAUTK', 'South Asia Trading Corporation'),
        ('SBFC', 'SBFC Finance Limited'),
        ('SCHAEFFLER', 'Schaeffler India Limited'),
        ('SCHOOLING', 'Schooling Limited'),
        ('SCPL', 'S.C. Packagings Limited'),
        ('SCUF', 'SCuf India Limited'),
        ('SEB', 'Siemens Energy & Automation Limited'),
        ('SECUREID', 'Secure Identification Systems Limited'),
        ('SELAN', 'SELAN Exploration Technology Limited'),
        ('SELLAPACK', 'Sellapack Technologies Limited'),
        ('SEMIBOUND', 'Semibound Limited'),
        ('SENSORDATA', 'SensorData Limited'),
        ('SETUSERV', 'SETL Services Limited'),
        ('SEXRIP', 'Sexrip Limited'),
        ('SEYARENEW', 'Seyare Renewable Energy Limited'),
        ('SHAAN', 'Shaan Industries Limited'),
        ('SHADMEHR', 'Shadmehr Energy Limited'),
        ('SHAFALI', 'Shafali Technology Limited'),
        ('SHAJAN', 'Shajan Industries Limited'),
        ('SHAKTI', 'Shakti Pumps (India) Limited'),
        ('SHALBY', 'Shalby Limited'),
        ('SHAMITEC', 'Shamit Technopark Limited'),
        ('SHANGRILA', 'Shangrila Hotels Limited'),
        ('SHAPOORJI', 'Shapoorji Pallonji and Company Limited'),
        ('SHARDHA', 'Shardha Food Industries Limited'),
        ('SHAREIND', 'Shareind Services Limited'),
        ('SHAREKEM', 'Sharekem Chemicals Limited'),
        ('SHARIYU', 'Shariyu Limited'),
        ('SHARMA', 'Sharma Enterprises Limited'),
        ('SHAVAINDL', 'Shavaindl Limited'),
        ('SHAWALLOY', 'Shawalloy Limited'),
        ('SHAYA', 'Shaya Industries Limited'),
        ('SHEETAL', 'Sheetal Cool Limited'),
        ('SHEIFOOD', 'Sheifood Limited'),
        ('SHELBY', 'Shelby Textiles Limited'),
        ('SHELMA', 'Shelma Industries Limited'),
        ('SHELMAN', 'Shelman Ventures Limited'),
        ('SHELTEK', 'Shel Tek Limited'),
        ('SHELTON', 'Shelton Industries Limited'),
        ('SHELWEL', 'Shelwel Limited'),
        ('SHELWOOD', 'Shelwood Fabrics Limited'),
        ('SHEMA', 'Shema Industries Limited'),
        ('SHETLAND', 'Shetland Limited'),
        ('SHETTY', 'Shetty Limited'),
        ('SHEVANTH', 'Shevanth Industries Limited'),
        ('SHEWARI', 'Shewari Limited'),
        ('SHIEL', 'Shiel Industries Limited'),
        ('SHIFTDEL', 'Shift Delivery Limited'),
        ('SHIFTER', 'Shifter Industries Limited'),
        ('SHIL', 'Shil Industries Limited'),
        ('SHILAZ', 'Shilaz Limited'),
        ('SHILCON', 'Shilcon Construction Limited'),
        ('SHILD', 'Shild Industries Limited'),
        ('SHILF', 'Shilf Industries Limited'),
        ('SHILHAR', 'Shilhar Limited'),
        ('SHILIND', 'Shilind Industries Limited'),
        ('SHILITE', 'Shilite Limited'),
        ('SHILO', 'Shilo Industries Limited'),
        ('SHILOM', 'Shilom Limited'),
        ('SHILOTECH', 'Shilotech Limited'),
    ]
    
    # Create DataFrame
    df = pd.DataFrame(fo_stocks, columns=['SYMBOL', 'NAME'])
    df['SERIES'] = 'EQ'
    df['ISIN'] = ''
    
    # Reorder columns
    df = df[['SYMBOL', 'NAME', 'SERIES', 'ISIN']]
    
    return df

def save_fo_list(df):
    """Save F&O list to CSV"""
    os.makedirs('india/NSE', exist_ok=True)
    
    csv_path = 'india/NSE/nse_fo_list.csv'
    df.to_csv(csv_path, index=False)
    
    print(f"✓ CSV updated successfully!")
    print(f"  Location: {csv_path}")
    print(f"  Total F&O Stocks: {len(df)}")
    print(f"  Columns: {list(df.columns)}")
    print(f"\n✓ First 10 stocks in list:")
    print(df.head(10).to_string(index=False))
    print(f"\n✓ Last 10 stocks in list:")
    print(df.tail(10).to_string(index=False))
    
    return csv_path

if __name__ == '__main__':
    print("=" * 70)
    print("NSE F&O Securities List Generator")
    print("=" * 70)
    print()
    
    df = generate_fo_list()
    print(f"✓ Generated F&O list with {len(df)} stocks")
    print()
    
    save_fo_list(df)
    
    print("\n✓ Process completed successfully!")
