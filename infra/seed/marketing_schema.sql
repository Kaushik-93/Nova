-- The marketing source. Money is decimal dollars; spend_date is in the ad
-- account's time zone, America/Los_Angeles.
DROP TABLE IF EXISTS ad_spend_daily;
DROP TABLE IF EXISTS campaigns;
DROP TABLE IF EXISTS channels;

CREATE TABLE channels (
    channel_code VARCHAR(32) PRIMARY KEY,
    channel_name VARCHAR(64) NOT NULL,
    is_paid      TINYINT(1) NOT NULL
);

CREATE TABLE campaigns (
    campaign_id        INT PRIMARY KEY,
    campaign_code      VARCHAR(80) NOT NULL UNIQUE,
    campaign_name      VARCHAR(120) NOT NULL,
    channel_code       VARCHAR(32) NOT NULL,
    target_region_code VARCHAR(16) NULL,
    start_date         DATE NOT NULL,
    end_date           DATE NULL,
    daily_budget_usd   DECIMAL(10, 2) NOT NULL,
    status             VARCHAR(16) NOT NULL,
    CONSTRAINT campaigns_channel_fk FOREIGN KEY (channel_code) REFERENCES channels (channel_code)
);

-- A day can have two rows for a campaign when the ad platform restates its
-- numbers. Only the row with is_latest = 1 is current.
CREATE TABLE ad_spend_daily (
    id                   BIGINT PRIMARY KEY,
    spend_date           DATE NOT NULL,
    campaign_id          INT NOT NULL,
    revision             TINYINT NOT NULL,
    is_latest            TINYINT(1) NOT NULL,
    impressions          INT NOT NULL,
    clicks               INT NOT NULL,
    spend_usd            DECIMAL(12, 2) NOT NULL,
    platform_conversions INT NOT NULL,
    synced_at            DATETIME NOT NULL,
    CONSTRAINT ad_spend_campaign_fk FOREIGN KEY (campaign_id) REFERENCES campaigns (campaign_id),
    UNIQUE KEY ad_spend_day_campaign_rev (spend_date, campaign_id, revision)
);
