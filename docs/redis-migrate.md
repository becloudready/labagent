# redis-migrate: check a legacy Azure Cache for Redis before moving it

Azure Cache for Redis is retiring: Basic, Standard and Premium on **2028-09-30**, Enterprise and Enterprise Flash on **2027-03-31**. Microsoft no longer lets new customers create one. This pack tells you, before you touch anything, whether a cache can be exported to Azure Managed Redis. Not Microsoft's tool.

```bash
az login
labagent launch redis-migrate --param source_cache=my-cache --param source_rg=my-rg
labagent launch redis-migrate --param source_cache=my-cache --param storage_account=mystorage --param target_region=eastus
```

Read only: it creates, changes and deletes nothing, so it needs no allow-list. It checks the cache, and the storage account if you give one. `--dry-run` shows the steps. Exit code 0 means no blockers (warnings may remain). Exit code 2 means blocked, and the error lists the blocker codes. Every finding is printed on stderr and saved in `~/.labagent/packs/redis-migrate/<name>.json`.

| Level | Meaning |
|---|---|
| blocker | export/import cannot work as asked |
| warning | it works, but something does not carry over |
| info | context |

**Blockers.** Basic or Standard tier (no export). Cache not in Succeeded state. VNet injection. Active geo-replication. Redis newer than 7.x. Storage account that is blob-only, Data Lake Gen2, or in another region than the cache.

**Warnings.** Only database 0 is migrated. Clustered cache, and cross-slot commands (Azure Managed Redis is clustered by default). Keyspace notifications, RDB/AOF persistence, private endpoints, the non-TLS port 6379. A storage firewall, or a storage account in another subscription (needs a managed identity). A target region where Azure Managed Redis is not listed.

**Not covered yet.** Enterprise and Enterprise Flash sources, the export, the import, and the checks after the import. Keys in databases above 0 cannot be counted without a Redis connection, so that is always a warning. Progress is tracked in [#36](https://github.com/becloudready/labagent/issues/36).

**How far to trust it.** The rules come from Microsoft's documentation and the field names from Azure's own SDK models. They have not been run against a real legacy cache. The import into Azure Managed Redis was tested with a Redis 6.2 snapshot (3,400 keys, every data type and TTL matched).
