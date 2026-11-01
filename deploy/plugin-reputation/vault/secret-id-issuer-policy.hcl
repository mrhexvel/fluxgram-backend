path "auth/approle/role/plugin-reputation-publisher/role-id" {
  capabilities = ["read"]
}

path "auth/approle/role/plugin-reputation-publisher/secret-id" {
  capabilities = ["update"]
  min_wrapping_ttl = "1s"
  max_wrapping_ttl = "5m"
}
