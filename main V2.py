import osmnx as ox
import networkx as nx
import pulp
from pulp import LpProblem, LpMinimize, LpVariable, lpSum, LpBinary
import random
import folium
from folium import Element
from geopy.distance import geodesic

# ---
# 1. CONFIGURAÇÃO E PARÂMETROS
# ---

print("Inicializando Otimização OpenRAN - Cenário Carnaval & REMESSA (V3 com Legenda)...")

random.seed(42)  # Reprodutibilidade

# --- 1.1 Coordenadas Fixas (Âncoras Reais) ---
LOCAIS_FIXOS = {
    "POLITECNICA": (-12.9996194, -38.5103449),  # Centro de Controle Acadêmico
    "STI_UFBA": (-13.0025, -38.5085),           # Nó Central REMESSA (Ondina)
    "IFBA_BARBALHO": (-12.9645, -38.5028),      # Nó Central REMESSA (Antigo CEFET)
    "FAROL_BARRA": (-13.0103, -38.5325),        # Referência Carnaval
    "ONDINA_FIM": (-13.0068, -38.5019)          # Referência Carnaval
}

PONTO_CENTRAL = LOCAIS_FIXOS["POLITECNICA"]

# --- 1.2 Parâmetros Geográficos ---
RAIO_AREA_KM = 4.5 
RAIO_AREA_METROS = RAIO_AREA_KM * 1000

# --- 1.3 Parâmetros da Rede ---
NUM_RUS_CARNAVAL = 15   # RUs concentradas no circuito
NUM_RUS_DISPERSAS = 15  # RUs espalhadas pela cidade
NUM_CANDIDATOS_DU = 60  
NUM_CANDIDATOS_CU_EXTRA = 3 

# --- 1.4 Custos ---
COSTO_INST_DU = 2000
COSTO_INST_CU_NOVA = 10000 
COSTO_INST_CU_EXISTENTE = 0  # Incentivo ao uso do STI/IFBA
COSTO_FIBRA_METRO = 1.5

# --- 1.5 Restrições de Latência/Distância (OpenRAN) ---
MAX_DIST_FH_METROS = 3500   # Fronthaul (RU -> DU)
MAX_DIST_MH_METROS = 9000   # Midhaul (DU -> CU)

# Capacidades
CAP_DU = 8  # RUs por DU
CAP_CU = 20 # DUs por CU

# ---
# 2. FUNÇÕES AUXILIARES
# ---

def calcular_distancia_geodesica(coord1, coord2):
    return geodesic(coord1, coord2).meters

def gerar_pontos_carnaval(inicio, fim, quantidade):
    pontos = []
    lat_step = (fim[0] - inicio[0]) / quantidade
    lon_step = (fim[1] - inicio[1]) / quantidade
    
    for i in range(quantidade):
        jitter_lat = random.uniform(-0.001, 0.001)
        jitter_lon = random.uniform(-0.001, 0.001)
        pontos.append((
            inicio[0] + lat_step * i + jitter_lat,
            inicio[1] + lon_step * i + jitter_lon
        ))
    return pontos

# ---
# 3. AQUISIÇÃO DE DADOS
# ---

print(f"Baixando malha viária (Raio: {RAIO_AREA_KM}km)...")
G = ox.graph_from_point(PONTO_CENTRAL, dist=RAIO_AREA_METROS, network_type='drive')
nodos_validos = list(G.nodes(data=True))

# 3.1 Gerar RUs (Demanda)
coords_carnaval = gerar_pontos_carnaval(LOCAIS_FIXOS["FAROL_BARRA"], LOCAIS_FIXOS["ONDINA_FIM"], NUM_RUS_CARNAVAL)
coords_dispersas = []
for _ in range(NUM_RUS_DISPERSAS):
    node = random.choice(nodos_validos)
    coords_dispersas.append((node[1]['y'], node[1]['x']))

lista_rus = coords_carnaval + coords_dispersas
print(f"Total de RUs geradas: {len(lista_rus)}")

# 3.2 Gerar Candidatos a DU
candidatos_du = []
for _ in range(NUM_CANDIDATOS_DU):
    node = random.choice(nodos_validos)
    candidatos_du.append((node[1]['y'], node[1]['x']))
candidatos_du.append(LOCAIS_FIXOS["STI_UFBA"])
candidatos_du.append(LOCAIS_FIXOS["IFBA_BARBALHO"])

# 3.3 Gerar Candidatos a CU
candidatos_cu_fixos = [LOCAIS_FIXOS["STI_UFBA"], LOCAIS_FIXOS["IFBA_BARBALHO"]]
candidatos_cu_novos = []
for _ in range(NUM_CANDIDATOS_CU_EXTRA):
    node = random.choice(nodos_validos)
    candidatos_cu_novos.append((node[1]['y'], node[1]['x']))

lista_candidatos_cu = candidatos_cu_fixos + candidatos_cu_novos

# ---
# 4. CÁLCULO DE CUSTOS E PRÉ-CHECK
# ---
print("Calculando matrizes de custos e verificando viabilidade...")

dist_ru_du = {}
dist_du_cu = {}

idx_ru = range(len(lista_rus))
idx_du = range(len(candidatos_du))
idx_cu = range(len(lista_candidatos_cu))

# RU -> DU
for i in idx_ru:
    du_viavel_encontrada = False
    for j in idx_du:
        dist = calcular_distancia_geodesica(lista_rus[i], candidatos_du[j])
        dist_real = dist * 1.3 
        dist_ru_du[(i, j)] = dist_real
        if dist_real <= MAX_DIST_FH_METROS:
            du_viavel_encontrada = True
    
    if not du_viavel_encontrada:
        print(f" ALERTA: RU {i} isolada. Movendo para perto do STI.")
        lista_rus[i] = LOCAIS_FIXOS["STI_UFBA"] 
        for j in idx_du:
            dist = calcular_distancia_geodesica(lista_rus[i], candidatos_du[j])
            dist_ru_du[(i, j)] = dist * 1.3

# DU -> CU
for j in idx_du:
    for k in idx_cu:
        dist = calcular_distancia_geodesica(candidatos_du[j], lista_candidatos_cu[k])
        dist_real = dist * 1.3
        dist_du_cu[(j, k)] = dist_real

# ---
# 5. OTIMIZAÇÃO (PLI)
# ---
print("Executando Solver de Otimização...")

prob = LpProblem("OpenRAN_Salvador_Carnaval", LpMinimize)

# Variáveis
y = LpVariable.dicts("DU_Ativa", idx_du, cat=LpBinary)
z = LpVariable.dicts("CU_Ativa", idx_cu, cat=LpBinary)
x = LpVariable.dicts("Link_RU_DU", [(i, j) for i in idx_ru for j in idx_du], cat=LpBinary)
w = LpVariable.dicts("Link_DU_CU", [(j, k) for j in idx_du for k in idx_cu], cat=LpBinary)

# Função Objetivo
custo_dus = lpSum(COSTO_INST_DU * y[j] for j in idx_du)
custo_cus = 0
for k in idx_cu:
    loc = lista_candidatos_cu[k]
    if loc in candidatos_cu_fixos:
        custo_cus += COSTO_INST_CU_EXISTENTE * z[k]
    else:
        custo_cus += COSTO_INST_CU_NOVA * z[k]

custo_fh = lpSum(dist_ru_du[i, j] * COSTO_FIBRA_METRO * x[i, j] for i in idx_ru for j in idx_du)
custo_mh = lpSum(dist_du_cu[j, k] * COSTO_FIBRA_METRO * w[j, k] for j in idx_du for k in idx_cu)

prob += custo_dus + custo_cus + custo_fh + custo_mh

# Restrições
for i in idx_ru:
    prob += lpSum(x[i, j] for j in idx_du) == 1

for j in idx_du:
    prob += lpSum(x[i, j] for i in idx_ru) <= CAP_DU * y[j]
    for i in idx_ru:
        prob += x[i, j] <= y[j]

for j in idx_du:
    prob += lpSum(w[j, k] for k in idx_cu) == y[j]

for k in idx_cu:
    prob += lpSum(w[j, k] for j in idx_du) <= CAP_CU * z[k]
    for j in idx_du:
        prob += w[j, k] <= z[k]

for i in idx_ru:
    for j in idx_du:
        if dist_ru_du[(i, j)] > MAX_DIST_FH_METROS:
            prob += x[i, j] == 0

for j in idx_du:
    for k in idx_cu:
        if dist_du_cu[(j, k)] > MAX_DIST_MH_METROS:
            prob += w[j, k] == 0

prob.solve()
print(f"Status: {pulp.LpStatus[prob.status]}")

# ---
# 6. VISUALIZAÇÃO E RESULTADOS
# ---
if pulp.LpStatus[prob.status] == "Optimal":
    print(f"Custo Total Estimado: R$ {pulp.value(prob.objective):,.2f}")
    
    m = folium.Map(location=PONTO_CENTRAL, zoom_start=13, tiles="CartoDB positron")

    # 6.1 Marcadores Especiais
    folium.Marker(LOCAIS_FIXOS["POLITECNICA"], tooltip="Escola Politécnica UFBA", icon=folium.Icon(color='purple', icon='graduation-cap', prefix='fa')).add_to(m)
    folium.Marker(LOCAIS_FIXOS["STI_UFBA"], tooltip="STI UFBA (Datacenter)", icon=folium.Icon(color='darkred', icon='server', prefix='fa')).add_to(m)
    folium.Marker(LOCAIS_FIXOS["IFBA_BARBALHO"], tooltip="IFBA Barbalho (Datacenter)", icon=folium.Icon(color='darkred', icon='server', prefix='fa')).add_to(m)

    # Backbone REMESSA
    folium.PolyLine([LOCAIS_FIXOS["STI_UFBA"], LOCAIS_FIXOS["IFBA_BARBALHO"]], color='black', weight=4, opacity=0.3, dash_array='10', tooltip="Backbone REMESSA").add_to(m)

    # 6.2 Elementos Ativos
    for k in idx_cu:
        if z[k].varValue > 0.9:
            loc = lista_candidatos_cu[k]
            cor = "red" if loc in candidatos_cu_fixos else "orange"
            folium.Marker(loc, popup="CU Ativa", icon=folium.Icon(color=cor, icon='cloud', prefix='fa')).add_to(m)

    for j in idx_du:
        if y[j].varValue > 0.9:
            loc_du = candidatos_du[j]
            folium.CircleMarker(loc_du, radius=6, color='blue', fill=True, fill_opacity=1, popup="DU OpenRAN").add_to(m)
            for k in idx_cu:
                if w[(j, k)].varValue > 0.9:
                    loc_cu = lista_candidatos_cu[k]
                    folium.PolyLine([loc_du, loc_cu], color='blue', weight=2.5, opacity=0.6, popup="Midhaul").add_to(m)

    for i in idx_ru:
        loc_ru = lista_rus[i]
        eh_carnaval = any(calcular_distancia_geodesica(loc_ru, c) < 50 for c in coords_carnaval)
        cor_ru = "green" if eh_carnaval else "gray"
        folium.CircleMarker(loc_ru, radius=4, color=cor_ru, fill=True, fill_opacity=0.8, popup="RU 5G").add_to(m)
        for j in idx_du:
            if x[(i, j)].varValue > 0.9:
                loc_du = candidatos_du[j]
                folium.PolyLine([loc_ru, loc_du], color='green', weight=1, opacity=0.5).add_to(m)

    # --- 6.3 ADICIONANDO LEGENDA FLUTUANTE (HTML/CSS) ---
    legend_html = '''
     <div style="
     position: fixed; 
     bottom: 50px; left: 50px; width: 280px; height: 360px; 
     background-color: white; border:2px solid grey; z-index:9999; font-size:14px;
     padding: 10px; border-radius: 10px; opacity: 0.9;
     font-family: sans-serif;
     ">
     <h4 style="margin-top:0;">Legenda OpenRAN</h4>
     <div style="margin-bottom: 5px;">
       <i class="fa fa-graduation-cap fa-lg" style="color:purple; width:20px; text-align:center;"></i> Escola Politécnica (UFBA)
     </div>
     <div style="margin-bottom: 5px;">
       <i class="fa fa-server fa-lg" style="color:darkred; width:20px; text-align:center;"></i> Datacenter (STI/IFBA)
     </div>
     <div style="margin-bottom: 5px;">
       <i class="fa fa-cloud fa-lg" style="color:red; width:20px; text-align:center;"></i> CU Ativa (REMESSA)
     </div>
     <div style="margin-bottom: 5px;">
       <i class="fa fa-cloud fa-lg" style="color:orange; width:20px; text-align:center;"></i> CU Ativa (Nova)
     </div>
     <div style="margin-bottom: 5px;">
       <i class="fa fa-circle fa-lg" style="color:blue; width:20px; text-align:center;"></i> DU Ativa
     </div>
     <div style="margin-bottom: 5px;">
       <i class="fa fa-circle fa-lg" style="color:green; width:20px; text-align:center;"></i> RU (Circuito Carnaval)
     </div>
     <div style="margin-bottom: 5px;">
       <i class="fa fa-circle fa-lg" style="color:gray; width:20px; text-align:center;"></i> RU (Dispersa na Cidade)
     </div>
     <hr>
     <div style="margin-bottom: 5px;">
       <span style="color:green; font-weight:bold;">&mdash;&mdash;</span> Link Fronthaul (RU-DU)
     </div>
     <div style="margin-bottom: 5px;">
       <span style="color:blue; font-weight:bold;">&mdash;&mdash;</span> Link Midhaul (DU-CU)
     </div>
     <div style="margin-bottom: 5px;">
       <span style="color:black; font-weight:bold; border-bottom: 2px dashed black;">&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;&nbsp;</span> Backbone REMESSA
     </div>
     </div>
     '''
    m.get_root().html.add_child(folium.Element(legend_html))

    output_file = "mapa_openran_carnaval_remessa.html"
    m.save(output_file)
    print(f"Mapa salvo em: {output_file}")

else:
    print("Solução Inviável. Verifique os alertas acima.")