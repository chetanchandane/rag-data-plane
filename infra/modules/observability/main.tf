# Slice 1 observability (SPEC: Infrastructure, "observability"): the DLQ alarm and the AWS
# Budget alerts. The API alarms (5xx rate, p95) come with the API in Slice 2. Log retention
# is set where each log group is created (the ingest module for the worker).

resource "aws_sns_topic" "alerts" {
  name = "${var.name_prefix}-alerts"
}

# AWS sends a confirmation email; alerts arrive only after the link in it is clicked.
resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

# Any message in the DLQ means an upload didn't get indexed: a corrupt file, a forged
# message, or a transient error that outlasted its retries. One is enough to alert.
resource "aws_cloudwatch_metric_alarm" "dlq_not_empty" {
  alarm_name        = "${var.name_prefix}-ingest-dlq-not-empty"
  alarm_description = "Messages in the ingest DLQ. Inspect each one's `reason` attribute, fix, then redrive."
  namespace         = "AWS/SQS"
  metric_name       = "ApproximateNumberOfMessagesVisible"
  dimensions = {
    QueueName = var.dlq_name
  }
  statistic           = "Maximum"
  period              = 60
  evaluation_periods  = 1
  comparison_operator = "GreaterThanThreshold"
  threshold           = 0
  treat_missing_data  = "notBreaching" # SQS sends no data points for an idle queue
  alarm_actions       = [aws_sns_topic.alerts.arn]
  ok_actions          = [aws_sns_topic.alerts.arn]
}

# SPEC: "AWS Budget alerts at $15 and $25". The budget covers the whole account's bill.
resource "aws_budgets_budget" "monthly" {
  name         = "${var.name_prefix}-monthly"
  budget_type  = "COST"
  limit_amount = "30"
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = [15, 25]
    content {
      notification_type          = "ACTUAL"
      comparison_operator        = "GREATER_THAN"
      threshold                  = notification.value
      threshold_type             = "ABSOLUTE_VALUE"
      subscriber_email_addresses = [var.alert_email]
    }
  }
}
