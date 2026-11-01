path "transit/keys/plugin-reputation" {
  capabilities = ["read"]
}

path "transit/sign/plugin-reputation/sha2-256" {
  capabilities = ["update"]

  required_parameters = [
    "input",
    "prehashed",
    "key_version",
    "marshaling_algorithm",
  ]

  allowed_parameters = {
    "input"     = []
    "prehashed" = [true, "true"]
    "*"         = []
  }
}

path "auth/token/lookup-self" {
  capabilities = ["read"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}
