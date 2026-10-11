-- Email-free registration also releases existing self-registered users waiting
-- for verification. Do not enable disabled users without a pending verify token.
WITH activated AS (
    UPDATE commerce.users u SET active=true
    WHERE u.role='user' AND NOT u.active AND NOT u.email_verified
      AND EXISTS (
          SELECT 1 FROM commerce.account_tokens t
          WHERE t.user_id=u.id AND t.purpose='verify' AND t.used_at IS NULL
      )
    RETURNING u.id
)
UPDATE commerce.account_tokens SET used_at=now()
WHERE user_id IN (SELECT id FROM activated) AND used_at IS NULL;
