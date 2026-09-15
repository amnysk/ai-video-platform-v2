-- integration テスト専用のDB（ADR-0021）。開発DB avp とは別。
-- docker-entrypoint-initdb.d は **データディレクトリが空の初回起動時だけ**実行される。
-- 既存ボリュームでは手で作る: docker compose exec postgres createdb -U avp avp_test
SELECT 'CREATE DATABASE avp_test'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'avp_test')\gexec
