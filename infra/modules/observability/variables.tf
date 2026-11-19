variable "name_prefix" {
  type = string
}

variable "dlq_name" {
  type = string
}

variable "alert_email" {
  description = "Where alarms and budget alerts go."
  type        = string
}
