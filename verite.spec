# Verite Configuration Specification file

[globals]
udp_port = integer(1000, 65535)
interfaces = string_list
secret = string
timestamp_format = string(default='%Y%m%d_%H%M%S')
endianness = option('big', 'little', default='big')
tcp_header_size = integer(1, 4, default=4)

[regex]
port_pattern = re(default='(?P<port>..)')
sms_pattern = re(default='IN(?P<timestamp>\d{8}_\d{6}).*(\+33|0)(?P<phone>\d{9}).*txt')
vote_pattern = re(default='^((?P<vrai>v(rai)?|o(ui)?)|f(aux)?|n(on)?)$')

[server]
inbox = directory

[display]
addr = ip_addr
port = integer(1000, 65535)

[client]

[sms_service]
fastapi_url = https_url
credits_url = https_url

    [[credentials]]
    accountid = string
    password = string(8, 8)

    [[send_options]]
    datacoding = option(0, 8, 16)
#    sender = string(max=11)
