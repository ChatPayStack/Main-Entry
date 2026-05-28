# Outlook Email Integration Setup

## Overview
This integration adds Outlook email webhook support to the FastAPI backend. Emails are automatically classified using OpenAI, and product enquiries are queued in Redis for processing.

## Required Environment Variables

Add these to your `.env` file:

```env
# Microsoft Graph API
GRAPH_API_ENDPOINT=https://graph.microsoft.com/v1.0
GRAPH_API_ACCESS_TOKEN=<your-microsoft-graph-access-token>
EMAIL_WEBHOOK_TOKEN=<your-webhook-validation-token>
```

## Setup Steps

### 1. Register Outlook Email Webhook with Microsoft Graph

Follow the [Microsoft Graph change tracking guide](https://learn.microsoft.com/en-us/graph/api/resources/webhooks) to set up notifications:

```bash
POST https://graph.microsoft.com/v1.0/subscriptions

{
  "changeType": "created",
  "notificationUrl": "https://your-domain.com/email-webhook",
  "resource": "/me/mailFolders('Inbox')/messages",
  "expirationDateTime": "2025-12-31T23:59:59Z",
  "clientState": "<your-email-webhook-token>"
}
```

### 2. Generate/Obtain Access Token

You need a Microsoft Graph access token with `Mail.Read` permission. This should be a long-lived token or obtained through a refresh token flow.

For service-to-service authentication:
- Use Client Credentials flow with Azure AD
- Request scope: `https://graph.microsoft.com/.default`

### 3. Test the Webhook

Your endpoint will receive a validation request:
```json
{
  "validationToken": "..."
}
```

The endpoint automatically returns this token to complete validation.

## API Endpoints

### Email Webhook
- **Endpoint**: `POST /email-webhook`
- **No authentication required** (Microsoft Graph webhook)
- **Handles**:
  - Webhook validation requests
  - Email change notifications
  - Full email fetching from Graph API
  - Classification via OpenAI
  - Redis queuing for product enquiries

## Data Flow

1. **Outlook** → Email arrives in inbox
2. **Microsoft Graph** → Sends webhook notification to `/email-webhook`
3. **Backend** → Fetches full email details from Graph API
4. **Extraction** → Extracts sender, subject, body, thread ID
5. **Classification** → OpenAI classifies as "product_enquiry" or "other"
6. **Queuing**:
   - If product enquiry → Push to `email_queue` Redis queue
   - If other → Log and skip (no queue)

## Redis Queue Format

When a product enquiry is detected, the payload is pushed to `email_queue`:

```json
{
  "channel": "email",
  "classification": "product_enquiry",
  "message": {
    "from": "sender@example.com",
    "from_name": "John Doe",
    "subject": "Product Inquiry",
    "body": "...",
    "thread_id": "AAMkADU1N2..."
  }
}
```

## Helper Functions

### `fetch_email_from_graph(mail_id, access_token)`
Fetches full email details from Microsoft Graph API.

**Returns**: Email data dict or None

### `extract_email_fields(email_data)`
Extracts sender, subject, body, and thread ID from email.

**Returns**: Dict with keys: `from`, `from_name`, `subject`, `body`, `thread_id`

### `classify_email_with_openai(subject, body)`
Uses GPT-4o-mini to classify email as product enquiry or other.

**Returns**: "product_enquiry" or "other"

## OpenAI Classification

The integration uses GPT-4o-mini for email classification with:
- Temperature: 0 (deterministic)
- Max tokens: 10
- System prompt focuses on binary classification

The classification looks for both "product_enquiry" and "product enquiry" variations.

## Error Handling

All errors are logged with prefixes for easy debugging:
- `ERR_EMAIL_WEBHOOK_JSON` - Failed to parse JSON
- `ERR_GRAPH_FETCH` - Failed to fetch from Graph API
- `ERR_FETCH_EMAIL_FROM_GRAPH` - Exception during Graph fetch
- `ERR_CLASSIFY_EMAIL` - Classification failed (defaults to "other")
- `ERR_EMAIL_WEBHOOK` - General webhook error
- `ERR_MISSING_GRAPH_ACCESS_TOKEN` - No access token configured

## Logging

Email processing includes detailed logging:
- Email ID on receipt
- Extracted email details (from, subject preview)
- Body preview (first 200 chars)
- Classification result
- Queue status (queued or skipped)

## Notes

- **No business_id**: Unlike WhatsApp/Telegram webhooks, emails don't use business_id routing
- **Email identification**: Email source is identified by Outlook inbox inbox context
- **Single tenant**: Currently designed for single mailbox (no multi-tenant support)
- **Access token**: Must be valid and have `Mail.Read` scope
- **Classification**: Defaults to "other" if OpenAI fails
- **Rate limiting**: Consider Microsoft Graph and OpenAI API rate limits for high-volume processing
