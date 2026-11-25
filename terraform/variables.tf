variable "project_id" {
  description = "Google Cloud Project ID hosting AskFinance Agent and Gemini Enterprise"
  type        = string
  default     = "your-gcp-project-id"
}

variable "region" {
  description = "Primary GCP region for Cloud Run, Artifact Registry, and Vertex AI"
  type        = string
  default     = "us-central1"
}

variable "service_name" {
  description = "Cloud Run v2 service name for AskFinance Agent"
  type        = string
  default     = "ask-finance-agent"
}

variable "container_image" {
  description = "Container image URI in Artifact Registry for Cloud Run deployment"
  type        = string
  default     = "us-central1-docker.pkg.dev/your-gcp-project-id/ask-finance-repo/ask-finance-agent:latest"
}

variable "min_instance_count" {
  description = "Minimum warm Cloud Run instances to eliminate live demo cold starts"
  type        = number
  default     = 1
}

variable "max_instance_count" {
  description = "Maximum autoscaled Cloud Run instances"
  type        = number
  default     = 10
}
