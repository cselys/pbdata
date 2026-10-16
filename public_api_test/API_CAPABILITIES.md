# Public.com API Capabilities Documentation Research

## Source
- Official documentation: https://public.com/api

## Authentication
- **Method**: Bearer token authentication
- **Header**: `Authorization: Bearer <API_KEY>`
- **API Key**: Obtained from https://public.com/api
- **Env Variable**: `PUBLIC_API_KEY`

*Note: This is based on typical API designs. Actual documentation should be verified.*

## Market Data Endpoints
*(To be verified against official documentation)*

### Available Endpoints (Preliminary)

1. **Quote/Quote Endpoint**
   - GET `/v1/quote` or similar
   - Single symbol quote retrieval
   - Returns: price, bid/ask, volume, timestamps

2. **Batch Quote Endpoint**
   - GET `/v1/quotes` or similar
   - Multiple symbols in one request
   - Supported batch sizes: to be verified

3. **Historical OHLCV Endpoint**
   - GET `/v1/historical` or similar
   - Supported intervals: 1m, 5m, 15m, 1d
   - Maximum lookback: to be verified

4. **Historical Endpoint**
   - GET `/v1/stocks/{symbol}/historical`
   - Or similar pattern

### Real-time vs Delayed Data
- **Documentation needed**: Whether data is real-time or delayed
- **Indicators**: Provider timestamp fields, delayed-data markers

### WebSocket/Streaming
- **Status**: To be verified against official docs
- **If supported**: Connection format, subscription messages

### Rate Limits
- **Status**: To be verified
- **Typical patterns**: Per-key, per-endpoint, per-account
- **429 responses**: Expected with Retry-After

### Market Session Coverage
- **US Market Hours**: 9:30 AM - 4:00 PM ET (Monday-Friday)
- **Pre-market/After-hours**: To be verified

### Supported Symbols
- US equities: AAPL, NVDA, MSFT, TSLA, SPY (example symbols)
- Extended hours: To be verified

## Timestamp and Freshness Documentation
- **Provider timestamp**: Field name and format to be verified
- **Data age calculation**: Current time vs provider timestamp
- **Delayed indicators**: Explicit markers in response

## Batch Quote Capabilities
- **Single symbol endpoint**: Expected
- **Batch endpoint**: To be verified
- **Max batch size**: To be verified

## Historical Data Intervals
- **1 minute**: To be verified
- **5 minutes**: To be verified
- **15 minutes**: To be verified
- **1 day**: To be verified

## Restrictions and Licensing
- **Per-api-key limits**: To be verified
- **Commercial use**: To be verified
- **Data redistribution**: To be verified

---
*This document will be updated as we verify each capability through the project implementation.*