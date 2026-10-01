"""Local adapter timeouts, token parameters, and API error tokens."""

W_PARAM = bytes.fromhex("44c73283b498d432ff25f5c8e06a016aef931e68f0a00ea710e36e6338fb22db")
S_PARAM = 0

CONNECT_TIMEOUT = 1.2
READ_TIMEOUT = 8.0

SERIALIZER_ERROR = "serializer_error"
DEVICE_AUTHENTICATION_ERROR = "device_authentication_error"
NO_MEMORY = "__no_memory"
SET_NO_SUCH_OPTION = "__set_no_such_option"
SET_OUT_OF_BOUNDS = "__set_out_of_bounds"
