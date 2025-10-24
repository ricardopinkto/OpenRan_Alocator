import subprocess
import iperf3
import time

# Configurações
RU_IP = "192.168.1.10"  # IP da Radio Unit
DU_IP = "192.168.1.20"  # IP da Distributed Unit
TEST_DURATION = 10      # duração do teste em segundos

def ping_test(target_ip):
    print(f"🔍 Testando latência para {target_ip}...")
    result = subprocess.run(["ping", "-c", "4", target_ip], capture_output=True, text=True)
    print(result.stdout)

def throughput_test(server_ip):
    print(f"🚀 Testando throughput com {server_ip}...")
    client = iperf3.Client()
    client.server_hostname = server_ip
    client.duration = TEST_DURATION
    result = client.run()
    if result.error:
        print(f"Erro no teste: {result.error}")
    else:
        print(f"Taxa de download: {result.received_Mbps} Mbps")
        print(f"Taxa de upload: {result.sent_Mbps} Mbps")

if __name__ == "__main__":
    print("=== Testes Open RAN 5G ===")
    ping_test(RU_IP)
    ping_test(DU_IP)
    throughput_test(DU_IP)