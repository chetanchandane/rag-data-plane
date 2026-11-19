account_id         = "905418224080"
region             = "us-east-2"
embedding_model_id = "amazon.titan-embed-text-v2:0"

# No browser client yet: uploads come from curl or the control plane's backend.
upload_cors_origins = []

# worker_image_tag: passed per deploy (-var worker_image_tag=<git sha>).
# alert_email:      set TF_VAR_alert_email in the shell or the GitHub environment.
