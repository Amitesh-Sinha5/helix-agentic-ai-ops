output "frontend_url" {
  value = "http://${aws_eip.helix.public_ip}:5173"
}

output "api_url" {
  value = "http://${aws_eip.helix.public_ip}:8000"
}

output "public_ip" {
  value = aws_eip.helix.public_ip
}

output "next_steps" {
  value = <<-EOT
    The instance builds both images on first boot; give it 5-10 minutes.

      watch -n 20 curl -fsS http://${aws_eip.helix.public_ip}:8000/health

    Then:
      1. Open http://${aws_eip.helix.public_ip}:5173 and SIGN UP IMMEDIATELY.
         The first account created becomes the administrator.
      2. Verify:  python scripts/smoke_test.py http://${aws_eip.helix.public_ip}:8000
      3. Watch the build:  ssh ec2-user@${aws_eip.helix.public_ip} 'tail -f /var/log/helix-boot.log'

    Costs nothing while inside the 12-month free tier. After that this instance
    is about $7.50/month -- the budget alarm emails you well before then.

    Tear down with:  terraform destroy
  EOT
}
