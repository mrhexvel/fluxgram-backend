exit_after_auth = false

vault {
  address = "https://vault:8200"
  ca_cert = "/vault/tls/vault-ca.pem"

  retry {
    num_retries = 5
  }
}

auto_auth {
  method "approle" {
    mount_path = "auth/approle"

    config = {
      role_id_file_path                   = "/vault/bootstrap/role-id"
      secret_id_file_path                 = "/vault/bootstrap/wrapped-secret-id"
      secret_id_response_wrapping_path    = "auth/approle/role/plugin-reputation-publisher/secret-id"
      remove_secret_id_file_after_reading = true
    }
  }

  sink "file" {
    config = {
      path  = "/vault/secrets/token"
      mode  = 0600
      owner = 65534
      group = 65534
    }
  }
}
