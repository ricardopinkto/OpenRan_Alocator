import osmnx as ox
import networkx as nx
import pulp
from pulp import LpProblem, LpMinimize, LpVariable, lpSum, LpBinary
import random
import folium
import time

# ---
# 1. PARÂMETROS DE SIMULAÇÃO (VALORES ARBITRADOS PARA O PCC)
# ---
# Estes são os valores que você irá justificar e talvez variar no seu TCC.

print("Inicializando script de otimização OpenRAN...")

# Semente para reprodutibilidade (para que os resultados sejam os mesmos toda vez)
random.seed(42)

# --- 1.1 Parâmetros Geográficos ---
# Ponto central da simulação (Escola Politécnica da UFBA)
# Geocodificação do local
try:
    local = ox.geocode("Escola Politécnica da UFBA, Salvador, Brazil")
    PONTO_CENTRAL = (local[0], local[1])
    print(f"Coordenadas do ponto central (Poli-UFBA) obtidas: {PONTO_CENTRAL}")
except Exception as e:
    print(f"Erro ao geocodificar local. Usando coordenadas fixas. Erro: {e}")
    # Coordenadas (Lat, Lon) da Escola Politécnica da UFBA
    PONTO_CENTRAL = (-12.9904, -38.5130)

# !! PARÂMETROS RELAXADOS (ÁREA MAIOR) !!
RAIO_AREA_KM = 2.0  # ANTERIOR: 1.5
RAIO_AREA_METROS = RAIO_AREA_KM * 1000

# --- 1.2 Parâmetros da Rede (Arbitrados) ---
# Quantidade de elementos a serem distribuídos no mapa
NUM_RUS = 20  # Número de Unidades de Rádio (antenas) a serem atendidas
NUM_CANDIDATOS_DU = 20  # ANTERIOR: 15 (Mais opções para o solver)
NUM_CANDIDATOS_CU = 8   # ANTERIOR: 6  (Mais opções para o solver)

# --- 1.3 Parâmetros de Custo (Arbitrados) ---
# Custos de instalação e operação (unidades monetárias fictícias)
COSTO_INST_DU = 1000  # Custo de ativação de um site de DU
COSTO_INST_CU = 5000  # Custo de ativação de um site de CU (mais caro/centralizado)
COSTO_LINK_FH = 2     # Custo por metro do link de Fronthaul (RU -> DU)
COSTO_LINK_MH = 1     # Custo por metro do link de Midhaul (DU -> CU)

# --- 1.4 Parâmetros de Restrição (Baseado nos Artigos) ---
# !! PARÂMETROS AGRESSIVAMENTE RELAXADOS !!

# Distância/Latência do Fronthaul
MAX_DIST_FH_METROS = 2000  # ANTERIOR: 1200

# Distância/Latência do Midhaul
MAX_DIST_MH_METROS = 4000 # ANTERIOR: 2500

# Capacidade dos equipamentos
CAP_DU = 7  # ANTERIOR: 6
CAP_CU = 6  # ANTERIOR: 5

# ---
# 2. FUNÇÃO AUXILIAR PARA CÁLCULO DE DISTÂNCIA
# ---
def get_street_distance(graph, node1, node2):
    """
    Calcula a distância mais curta pela malha viária entre dois nós do grafo.
    """
    try:
        # Usa o algoritmo de Dijkstra (padrão do networkx) com peso 'length'
        length = nx.shortest_path_length(graph, node1, node2, weight='length')
        return length
    except nx.NetworkXNoPath:
        # Se não houver caminho de rua (ex: ilhas desconectadas no grafo)
        # Retorna um valor "infinito" para tornar esse link inviável
        return 1e9 # 1 bilhão de metros

# ---
# 3. AQUISIÇÃO E PREPARAÇÃO DOS DADOS GEOGRÁFICOS (OSMnx)
# ---

print(f"Baixando malha viária de {RAIO_AREA_KM}km ao redor do ponto central...")
# Baixa o grafo da malha viária (ruas)
# 'drive' inclui apenas ruas acessíveis por carros
G = ox.graph_from_point(PONTO_CENTRAL, dist=RAIO_AREA_METROS, network_type='drive')
# Converte para um grafo não-direcionado para facilitar o roteamento
G_undir = G.to_undirected()
print("Malha viária baixada e processada.")

# Os nós do grafo (cruzamentos) serão nossos pontos de interesse
lista_de_nos = list(G_undir.nodes())

# --- 3.1 Seleção dos locais (RUs e Candidatos) ---
# Como não temos dados reais de onde estão as antenas, vamos selecioná-las
# aleatoriamente dentre os nós do grafo (cruzamentos) para a simulação.

# Garante que temos nós suficientes no grafo
total_necessario = NUM_RUS + NUM_CANDIDATOS_DU + NUM_CANDIDATOS_CU
if len(lista_de_nos) < total_necessario:
    print(f"Erro: O grafo da área selecionada tem apenas {len(lista_de_nos)} nós (necessários: {total_necessario}).")
    print("Reduza o número de RUs/Candidatos ou aumente o raio da área.")
    exit()

# Seleciona aleatoriamente os nós do grafo
random.shuffle(lista_de_nos)
nos_ru = lista_de_nos[:NUM_RUS]
nos_du_candidatos = lista_de_nos[NUM_RUS : NUM_RUS + NUM_CANDIDATOS_DU]
nos_cu_candidatos = lista_de_nos[NUM_RUS + NUM_CANDIDATOS_DU : total_necessario]

print(f"Elementos da rede definidos:")
print(f"  {len(nos_ru)} RUs")
print(f"  {len(nos_du_candidatos)} Locais candidatos para DU")
print(f"  {len(nos_cu_candidatos)} Locais candidatos para CU")

# ---
# 4. PRÉ-CÁLCULO DAS DISTÂNCIAS
# ---
# Para não sobrecarregar o solver, calculamos todas as distâncias de antemão.

print("Pré-calculando matriz de distâncias... (Isso pode demorar um pouco)")
start_time = time.time()

# Distâncias RU -> DU (Fronthaul)
dist_ru_du = {}
for i in nos_ru:
    for j in nos_du_candidatos:
        dist_ru_du[i, j] = get_street_distance(G_undir, i, j)

# Distâncias DU -> CU (Midhaul)
dist_du_cu = {}
for j in nos_du_candidatos:
    for k in nos_cu_candidatos:
        dist_du_cu[j, k] = get_street_distance(G_undir, j, k)

end_time = time.time()
print(f"Distâncias calculadas em {end_time - start_time:.2f} segundos.")

# ---
# 5. FORMULAÇÃO DO PROBLEMA (PuLP)
# ---
# Esta seção implementa o modelo matemático que definimos no LaTeX.

print("Formulando o problema de Programação Linear Inteira...")

# 5.1 Inicialização do Problema
# Queremos minimizar o custo total
prob = LpProblem("Otimizacao_OpenRAN_PCC", LpMinimize)

# 5.2 Definição das Variáveis de Decisão
# Variáveis Binárias (0 ou 1) que o solver irá decidir

# y_j: 1 se a DU for instalada no local candidato j
y = LpVariable.dicts("DU_Ativada", nos_du_candidatos, cat=LpBinary)

# z_k: 1 se a CU for instalada no local candidato k
z = LpVariable.dicts("CU_Ativada", nos_cu_candidatos, cat=LpBinary)

# As chaves (keys) para as variáveis x e w devem ser os pares (i,j) e (j,k).
# O método LpVariable.dicts precisa de uma lista desses pares (tuplas).
# Vamos pegar esses pares diretamente dos dicionários de distância que já criamos.

x_keys = dist_ru_du.keys()
w_keys = dist_du_cu.keys()

# x_ij: 1 se a RU i for conectada à DU j
x = LpVariable.dicts("Link_RU_DU", x_keys, cat=LpBinary)

# w_jk: 1 se a DU j for conectada à CU k
w = LpVariable.dicts("Link_DU_CU", w_keys, cat=LpBinary)


# 5.3 Definição da Função Objetivo
# Custo Total = Custo de Instalação + Custo dos Links
# Inspirado nos artigos (minimizar custos de ativação e número de PPs ativos)
custo_instalacao_du = lpSum(COSTO_INST_DU * y[j] for j in nos_du_candidatos)
custo_instalacao_cu = lpSum(COSTO_INST_CU * z[k] for k in nos_cu_candidatos)

# Agora o acesso x[i, j] (que é x[(i,j)]) vai funcionar, pois as chaves de x
# são as mesmas chaves de dist_ru_du.
custo_links_fh = lpSum(dist_ru_du[i, j] * COSTO_LINK_FH * x[i, j]
                       for (i, j) in x_keys)
                       
custo_links_mh = lpSum(dist_du_cu[j, k] * COSTO_LINK_MH * w[j, k]
                       for (j, k) in w_keys)

# Adiciona a função objetivo completa ao problema
prob += (custo_instalacao_du + custo_instalacao_cu + 
         custo_links_fh + custo_links_mh), "Custo Total da Rede"

# 5.4 Definição das Restrições
print("Adicionando restrições ao modelo...")

# Restrição 1: Cada RU deve ser atendida por exatamente uma DU.
for i in nos_ru:
    # Soma todas as conexões X saindo da RU 'i'
    prob += lpSum(x[i, j] for j in nos_du_candidatos if (i, j) in x_keys) == 1, f"Atendimento_RU_{i}"

# Restrição 2: Uma RU só pode se conectar a uma DU se ela estiver ativa (y[j]=1).
for (i, j) in x_keys:
    prob += x[i, j] <= y[j], f"Ativacao_Link_RU_DU_{i}_{j}"

# Restrição 3: Cada DU ATIVA (y[j]=1) deve ser conectada a exatamente uma CU.
for j in nos_du_candidatos:
    # Soma todas as conexões W saindo da DU 'j'
    prob += lpSum(w[j, k] for k in nos_cu_candidatos if (j, k) in w_keys) == y[j], f"Atendimento_DU_{j}"

# Restrição 4: Uma DU só pode se conectar a uma CU se a CU estiver ativa (z[k]=1).
for (j, k) in w_keys:
    prob += w[j, k] <= z[k], f"Ativacao_Link_DU_CU_{j}_{k}"

# Restrição 5: Capacidade da DU.
for j in nos_du_candidatos:
    # Soma todas as conexões X entrando na DU 'j'
    prob += lpSum(x[i, j] for i in nos_ru if (i, j) in x_keys) <= CAP_DU * y[j], f"Capacidade_DU_{j}"

# Restrição 6: Capacidade da CU.
for k in nos_cu_candidatos:
    # Soma todas as conexões W entrando na CU 'k'
    prob += lpSum(w[j, k] for j in nos_du_candidatos if (j, k) in w_keys) <= CAP_CU * z[k], f"Capacidade_CU_{k}"

# Restrição 7: Distância/Latência do Fronthaul (RU -> DU).
# Um link x[i,j] só pode ser 1 se a distância for menor que o limite.
for (i, j) in x_keys:
    if dist_ru_du[i, j] > MAX_DIST_FH_METROS:
        # Se a distância for maior que o limite, esse link é proibido.
        prob += x[i, j] == 0, f"Restricao_Dist_FH_{i}_{j}"

# Restrição 8: Distância/Latência do Midhaul (DU -> CU).
for (j, k) in w_keys:
    if dist_du_cu[j, k] > MAX_DIST_MH_METROS:
        # Se a distância for maior que o limite, esse link é proibido.
        prob += w[j, k] == 0, f"Restricao_Dist_MH_{j}_{k}"

print("Formulação do problema concluída.")

# ---
# 6. RESOLUÇÃO DO PROBLEMA
# ---
print("\n--- Iniciando Solver PuLP ---")
# O solver (CBC) tentará encontrar a solução ótima
# Aumentar o raio e o nro de candidatos torna o problema maior, 
# então podemos dar mais tempo ao solver se necessário.
# prob.solve(pulp.PULP_CBC_CMD(timeLimit=60)) # Ex: 60 segundos de limite
prob.solve()
print(f"Status da Solução: {pulp.LpStatus[prob.status]}")

# ---
# 7. APRESENTAÇÃO DOS RESULTADOS (Terminal)
# ---

if pulp.LpStatus[prob.status] == "Optimal":
    print(f"Custo Total Ótimo Encontrado: {pulp.value(prob.objective):.2f}")

    print("\n--- DUs Ativadas ---")
    d_ativas = []
    for j in nos_du_candidatos:
        if y[j].varValue == 1:
            d_ativas.append(j)
            print(f"  [+] DU ativada no nó: {j}")
    print(f"Total de DUs ativadas: {len(d_ativas)} / {len(nos_du_candidatos)}")

    print("\n--- CUs Ativadas ---")
    c_ativas = []
    for k in nos_cu_candidatos:
        if z[k].varValue == 1:
            c_ativas.append(k) # <-- Corrigido um bug aqui (estava 'j')
            print(f"  [+] CU ativada no nó: {k}")
    print(f"Total de CUs ativadas: {len(c_ativas)} / {len(nos_cu_candidatos)}")

    print("\n--- Conexões da Rede ---")
    conexoes_ru_du = []
    for (i, j) in x_keys:
        if x[i, j].varValue == 1:
            conexoes_ru_du.append((i, j))
            print(f"  RU {i}  == (Fronthaul) ==> DU {j}  (Dist: {dist_ru_du[i, j]:.0f}m)")

    conexoes_du_cu = []
    for (j, k) in w_keys:
        if w[j, k].varValue == 1:
            conexoes_du_cu.append((j, k))
            print(f"  DU {j}  == (Midhaul)   ==> CU {k}  (Dist: {dist_du_cu[j, k]:.0f}m)")
                
elif pulp.LpStatus[prob.status] == "Infeasible":
    print("O problema é INVIÁVEL.")
    print("Isso significa que não há solução que atenda a todas as restrições.")
    print("Possíveis causas:")
    print("  1. Os limites de distância (MAX_DIST_FH, MAX_DIST_MH) são muito rígidos.")
    print("  2. As capacidades (CAP_DU, CAP_CU) são muito baixas para o número de RUs.")
    print("  3. Não há locais candidatos suficientes.")
    print("  4. (Sorteio) Uma RU foi sorteada em um 'beco sem saída' do mapa (ilha).")
    print("\nTente relaxar as restrições e rodar novamente.")
else:
    print("O solver não encontrou uma solução ótima.")


# ---
# 8. VISUALIZAÇÃO DOS RESULTADOS (Folium)
# ---
if pulp.LpStatus[prob.status] == "Optimal":
    print("\nGerando mapa de visualização...")
    
    # Cria o mapa centrado no local
    m = folium.Map(location=PONTO_CENTRAL, zoom_start=14, tiles="CartoDB positron") # Zoom 14 para a área maior

    # Adiciona a malha viária ao mapa
    folium.GeoJson(ox.graph_to_gdfs(G_undir, edges=True, nodes=False), 
                   style_function=lambda x: {'color': '#999999', 'weight': 1, 'opacity': 0.5}).add_to(m)

    # Função para obter coordenadas (lat, lon) de um nó
    def get_coords(node):
        return (G_undir.nodes[node]['y'], G_undir.nodes[node]['x'])

    # Desenha os links de Fronthaul (RU -> DU)
    for (i, j) in conexoes_ru_du:
        try:
            path = nx.shortest_path(G_undir, i, j, weight='length')
            coords = [get_coords(n) for n in path]
            folium.PolyLine(coords, color='blue', weight=2, opacity=0.7, 
                            popup=f"FH: {i} -> {j}\nDist: {dist_ru_du[i, j]:.0f}m").add_to(m)
        except nx.NetworkXNoPath:
            print(f"Aviso: Não foi possível desenhar rota para FH {i} -> {j}")

    # Desenha os links de Midhaul (DU -> CU)
    for (j, k) in conexoes_du_cu:
        try:
            path = nx.shortest_path(G_undir, j, k, weight='length')
            coords = [get_coords(n) for n in path]
            folium.PolyLine(coords, color='red', weight=3, opacity=0.8, 
                            popup=f"MH: {j} -> {k}\nDist: {dist_du_cu[j, k]:.0f}m").add_to(m)
        except nx.NetworkXNoPath:
            print(f"Aviso: Não foi possível desenhar rota para MH {j} -> {k}")

    # Adiciona os marcadores dos elementos
    
    # RUs
    for i in nos_ru:
        folium.Marker(
            location=get_coords(i),
            tooltip=f"RU (Nó: {i})",
            icon=folium.Icon(color='green', icon='broadcast-tower', prefix='fa')
        ).add_to(m)

    # DUs (Ativas e Inativas)
    for j in nos_du_candidatos:
        if j in d_ativas:
            folium.Marker(
                location=get_coords(j),
                tooltip=f"DU ATIVADA (Nó: {j})",
                icon=folium.Icon(color='blue', icon='server', prefix='fa')
            ).add_to(m)
        else:
            folium.Marker(
                location=get_coords(j),
                tooltip=f"Candidato DU Inativo (Nó: {j})",
                icon=folium.Icon(color='gray', icon='server', prefix='fa'),
                opacity=0.5
            ).add_to(m)

    # CUs (Ativas e Inativas)
    for k in nos_cu_candidatos:
        if k in c_ativas:
            folium.Marker(
                location=get_coords(k),
                tooltip=f"CU ATIVADA (Nó: {k})",
                icon=folium.Icon(color='red', icon='database', prefix='fa')
            ).add_to(m)
        else:
            folium.Marker(
                location=get_coords(k),
                tooltip=f"Candidato CU Inativo (Nó: {k})",
                icon=folium.Icon(color='gray', icon='database', prefix='fa'),
                opacity=0.5
            ).add_to(m)

    # Salva o mapa em um arquivo HTML
    output_filename = "mapa_solucao_openran.html"
    m.save(output_filename)
    print(f"\nMapa salvo com sucesso em: {output_filename}")
    print("Abra este arquivo em seu navegador para ver a solução.")
    
    # Adiciona try/except para o desenho das rotas,
    # pois get_street_distance pode retornar 'infinito' e nx.shortest_path falharia
    # Também ajustei o zoom do mapa para 14, para vermos a área maior.
    # Corrigi um pequeno bug de copiar/colar (c_ativas.append(j) virou c_ativas.append(k))

print("\nScript concluído.")