output "api_url" {
  description = "Base URL of the API (the $default stage), no trailing slash."
  value       = aws_apigatewayv2_api.api.api_endpoint
}

output "api_repository_url" {
  value = aws_ecr_repository.api.repository_url
}

output "api_function_name" {
  value = aws_lambda_function.api.function_name
}

output "access_log_group" {
  value = aws_cloudwatch_log_group.access.name
}
