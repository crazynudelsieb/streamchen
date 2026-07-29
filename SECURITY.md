# Security Policy

## 🛡️ Supported Versions

We actively maintain and provide security updates for the following versions:

| Version | Supported          |
| ------- | ------------------ |
| Latest  | ✅ Yes             |
| < Latest| ❌ No              |

**Note:** As this is a single-branch project, we only support the latest version on the `main` branch.

## 🚨 Reporting a Vulnerability

**Please do NOT report security vulnerabilities through public GitHub issues.**

### How to Report

**Email:** Please use GitHub's security reporting feature or contact the project maintainer

**What to Include:**
- Description of the vulnerability
- Steps to reproduce (if applicable)
- Potential impact assessment
- Any suggested fixes or mitigations

### Response Timeline

- **Initial Response:** Within 48 hours
- **Status Update:** Within 7 days
- **Resolution Target:** 30 days for critical issues

### What to Expect

1. **Acknowledgment:** We'll confirm receipt of your report
2. **Investigation:** We'll investigate and assess the severity
3. **Communication:** We'll keep you updated on progress
4. **Resolution:** We'll develop and test a fix
5. **Disclosure:** We'll coordinate responsible disclosure

## 🔒 Security Best Practices

### For Users

**Environment Configuration:**
- Use strong, unique `SECRET_KEY` (32+ characters)
- Use strong database passwords
- Enable HTTPS in production
- Keep email credentials secure
- Regularly update Docker images

**Deployment Security:**
- Run containers as non-root user
- Use environment variables for secrets
- Regularly update dependencies
- Monitor application logs
- Implement proper backup strategies

### For Developers

**Code Security:**
- Never commit credentials or API keys
- Validate all user inputs
- Use parameterized database queries
- Implement proper error handling
- Follow secure coding practices

**Dependencies:**
- Regularly update Python packages
- Monitor for security advisories
- Use dependency scanning tools
- Pin versions in production

## 🛠️ Security Features

### Application Security

**Input Validation:**
- Email address validation and sanitization
- CSRF protection on all forms
- XSS prevention through template escaping
- SQL injection prevention via SQLAlchemy ORM

**Session Security:**
- Secure session configuration
- HTTPOnly cookies
- SameSite cookie policy
- Secure headers implementation

**Email Security:**
- Email address validation before sending
- Proper SMTP authentication
- TLS encryption for email transport
- No user input in email headers

### Infrastructure Security

**Docker Security:**
- Non-root container execution
- Minimal base images
- No unnecessary capabilities
- Environment-based configuration

**Database Security:**
- Connection encryption
- Parameterized queries only
- No direct SQL execution
- Proper access controls

## 🔍 Known Security Considerations

### Current Limitations

1. **Token-Based Authentication:** Share tokens provide access to groups
   - **Mitigation:** Use strong random token generation, educate users about sharing responsibly

2. **Email-Based Features:** Email addresses are stored for invitations
   - **Mitigation:** Minimal data collection, proper validation, optional feature

3. **No User Authentication:** No traditional user accounts
   - **Design Choice:** Privacy-first approach, users control their own access

### Security Assumptions

- Users are responsible for sharing group tokens securely
- Email infrastructure is properly configured and secured
- Docker/container runtime environment is secure
- Network security is handled at infrastructure level

## 📋 Security Checklist for Deployments

### Pre-Deployment

- [ ] Strong `SECRET_KEY` configured
- [ ] Database credentials are secure
- [ ] SMTP credentials are secure
- [ ] All environment variables configured
- [ ] HTTPS enabled (production)
- [ ] Container security verified

### Post-Deployment

- [ ] Application starts without errors
- [ ] Email functionality tested
- [ ] Security headers verified
- [ ] Database connections secured
- [ ] Log monitoring configured
- [ ] Backup strategy implemented

## 🚨 Incident Response

### If You Discover a Security Issue

1. **Immediate:** Stop the affected service if critical
2. **Document:** Record what you observed
3. **Report:** Follow our vulnerability reporting process
4. **Monitor:** Watch for any suspicious activity
5. **Communicate:** Inform relevant stakeholders

### Our Response Process

1. **Triage:** Assess severity and impact
2. **Contain:** Implement immediate mitigations
3. **Investigate:** Determine root cause
4. **Fix:** Develop and test solution
5. **Deploy:** Release security update
6. **Document:** Update security documentation

## 📚 Security Resources

### General Security

- [OWASP Top 10](https://owasp.org/www-project-top-ten/)
- [Flask Security Guide](https://flask.palletsprojects.com/en/2.3.x/security/)
- [Docker Security Best Practices](https://docs.docker.com/develop/dev-best-practices/)

### Python Security

- [Python Security Guide](https://python-security.readthedocs.io/)
- [Bandit Security Linter](https://bandit.readthedocs.io/)
- [Safety Package Scanner](https://pyup.io/safety/)

---

**Remember:** Security is a shared responsibility. While we work hard to build secure software, proper deployment and configuration are equally important.

For questions about this security policy, please open a GitHub Discussion or contact the project maintainer.