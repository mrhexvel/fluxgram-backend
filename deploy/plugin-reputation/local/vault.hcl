ui = false
disable_mlock = true
api_addr = "https://vault:8200"

storage "file" {
  path = "/vault/data"
}

listener "tcp" {
  address         = "0.0.0.0:8200"
  tls_cert_file   = "/vault/tls/vault-cert.pem"
  tls_key_file    = "/vault/tls/vault-key.pem"
  tls_min_version = "tls12"
}
