output "queue_url" {
  value = aws_sqs_queue.ingest.id
}

output "dlq_name" {
  value = aws_sqs_queue.dlq.name
}

output "dlq_url" {
  value = aws_sqs_queue.dlq.id
}

output "worker_repository_url" {
  value = aws_ecr_repository.worker.repository_url
}

output "worker_function_name" {
  value = aws_lambda_function.worker.function_name
}
