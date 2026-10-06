output "function_arn" {
  description = "The worker's ARN: what the pipeline's VISION_WORKER_ARN is set to, and what its role may invoke. common/vision_client.py reads the worker's Region from it."
  value       = aws_lambda_function.worker.arn
}

output "function_name" {
  value = aws_lambda_function.worker.function_name
}

output "region" {
  value = var.region
}
