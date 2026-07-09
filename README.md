# SSH-Based Network Automation and Orchestration Platform

A lightweight web-based Network Automation and Orchestration Platform developed using **Python**, **Flask**, **Netmiko**, **Jinja2**, and **SQLite**. The platform enables network administrators to centrally manage Cisco network devices through secure SSH connections, automate repetitive configuration tasks, monitor device health, and maintain configuration backups from a single dashboard.

---

## Features

* Secure SSH-based communication using Netmiko
* Centralized device inventory management
* Jinja2-based configuration templating
* Automated configuration deployment
* Configuration backup and snapshot management
* Real-time device monitoring (CPU, Memory, Interface Status)
* Rule-based user authentication (Admin, Operator, Viewer)
* Parallel configuration deployment using multithreading
* Configuration validation and error handling
* Job execution logging and status reporting
* Responsive Flask web dashboard

---

## Technology Stack

### Backend

* Python 3
* Flask
* SQLAlchemy
* Netmiko
* Jinja2

### Frontend

* HTML5
* CSS3
* JavaScript
* Bootstrap

### Database

* SQLite

### Network Protocols

* SSH
* SNMP (Monitoring)

---

## Project Structure

```text
app/
│
├── api/                    # REST API endpoints
├── models/                 # Database models
├── services/               # Automation engine
│   ├── automation_service.py
│   ├── ssh_client.py
│   ├── monitor.py
│   ├── template_engine.py
│   └── validator.py
│
├── templates/
│   ├── config_templates/
│   └── pages/
│
├── static/
│   ├── css/
│   └── js/
│
├── routes.py
├── auth.py
└── config.py

tests/
instance/
run.py
requirements.txt
```

---

## Automation Workflow

1. User logs into the web application.
2. Administrator selects one or more network devices.
3. A configuration template is selected.
4. Required parameters are entered through the web interface.
5. Jinja2 renders the final Cisco IOS configuration.
6. Netmiko establishes a secure SSH connection.
7. Configuration commands are deployed automatically.
8. Running configuration is saved.
9. Job status and logs are displayed to the user.

---

## Example Template

### Jinja2 Template

```jinja
ip route {{ destination_network }} {{ subnet_mask }} {{ next_hop }}
```

### User Input

```text
Destination Network : 10.50.0.0
Subnet Mask         : 255.255.0.0
Next Hop            : 192.168.1.254
```

### Generated Configuration

```text
ip route 10.50.0.0 255.255.0.0 192.168.1.254
```

---

## Installation

Clone the repository:

```bash
git clone https://github.com/your-username/network-automation-platform.git
```

Navigate into the project:

```bash
cd network-automation-platform
```

Create a virtual environment:

```bash
python -m venv venv
```

Activate the virtual environment.

Windows

```bash
venv\Scripts\activate
```

Linux/macOS

```bash
source venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Configure environment variables:

```bash
cp .env.example .env
```

Run the application:

```bash
python run.py
```

---

## Key Modules

| Module                  | Description                                                |
| ----------------------- | ---------------------------------------------------------- |
| `automation_service.py` | Executes SSH automation tasks and configuration deployment |
| `ssh_client.py`         | Handles secure SSH communication with network devices      |
| `template_engine.py`    | Renders Jinja2 configuration templates                     |
| `monitor.py`            | Collects monitoring information from devices               |
| `validator.py`          | Validates user input before deployment                     |
| `job_worker.py`         | Executes automation jobs concurrently                      |

---

## Future Enhancements

* NETCONF and RESTCONF support
* YANG model integration
* Multi-vendor device support
* Role-Based Access Control (RBAC)
* Configuration rollback
* Scheduled automation jobs
* Docker deployment
* PostgreSQL/MySQL support
* REST API authentication
* Email and webhook notifications

---

## Educational Purpose

This project was developed as a final-year undergraduate project to demonstrate practical applications of network automation, orchestration, configuration management, and centralized network administration using Python.

---

## License

This project is intended for educational and research purposes. Feel free to modify and extend it for learning or personal use.

---

## Author

**Your Name**

Bachelor of Computer Science and Information Technology (BSc CSIT)

Final Year Project
