variable "region" {
  description = "AWS region. ap-south-1 is closest to Bengaluru."
  type        = string
  default     = "ap-south-1"
}

variable "instance_type" {
  description = "Free-tier eligible: t3.micro or t2.micro (1 GB RAM). t3.small has 2 GB but is NOT free."
  type        = string
  default     = "t3.micro"
}

variable "swap_gb" {
  description = "Swap file size. 1 GB of RAM is genuinely tight for this stack; swap is what makes it survive."
  type        = number
  default     = 4
}

variable "alert_email" {
  description = "Where the spend alarm goes. Required — 'free' only stays free if you get told when it isn't."
  type        = string

  validation {
    condition     = can(regex("^[^@]+@[^@]+\\.[^@]+$", var.alert_email))
    error_message = "alert_email must be a valid email address."
  }
}

variable "ssh_cidr" {
  description = "Your IP as a /32 to allow SSH, e.g. 203.0.113.4/32. Empty disables SSH entirely."
  type        = string
  default     = ""
}

variable "key_name" {
  description = "Existing EC2 key pair name for SSH. Empty means no key (use EC2 Instance Connect instead)."
  type        = string
  default     = ""
}

variable "repo_url" {
  description = "Repository the instance clones on boot."
  type        = string
  default     = "https://github.com/Amitesh-Sinha5/helix-agentic-ai-ops.git"
}

variable "llm_provider" {
  description = "mock keeps it free and offline. A hosted provider costs money per call."
  type        = string
  default     = "mock"
}
