account_id         = "905418224080"
region             = "us-east-2"
embedding_model_id = "amazon.titan-embed-text-v2:0"

# Answers (step 15): Nova 2 Lite through its US inference profile, falling back to Qwen3 in
# this Region. Haiku 5.5 is refused for this account (DECISIONS 2026-11-24, "LLMs without
# Anthropic"); switching back is these two lines.
llm_primary_model_id   = "us.amazon.nova-2-lite-v1:0"
llm_secondary_model_id = "qwen.qwen3-235b-a22b-2507-v1:0"

# No browser client yet: uploads come from curl or the control plane's backend.
upload_cors_origins = []

# worker_image_tag, api_image_tag: passed per deploy (-var ...=<git sha>).
# alert_email:      set TF_VAR_alert_email in the shell or the GitHub environment.
