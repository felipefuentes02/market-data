import json, csv
from django.http import JsonResponse, HttpResponse
from django.utils import timezone
from django.db import transaction
from django.views.decorators.csrf import csrf_exempt
from .models import Producto, Venta, DetalleVenta, Inventario, Tienda, Usuario, DetalleFactura, ClienteFiado, AbonoFiado, CajaSesion, Factura, Comuna
from django.shortcuts import render, redirect
from django.core.mail import send_mail
import random, string
from django.contrib import messages
from django.db.models.functions import TruncDate
from datetime import timedelta
from django.db.models import Sum, F, Avg, ExpressionWrapper, DecimalField, Q
from django.core.paginator import Paginator

def buscar_producto_por_codigo(request):
    """ Busca por código exacto, extrayendo el precio exclusivamente del inventario. """
    codigo = request.GET.get('codigo', None)
    rut_tienda = request.GET.get('rut_tienda') or request.session.get('rut_tienda')
    
    if not codigo:
        return JsonResponse({'error': 'No se proporcionó un código de barras'}, status=400)

    try:
        producto = Producto.objects.filter(cod_barra=codigo).first()
        if not producto:
            return JsonResponse({'error': 'Producto no encontrado'}, status=404)
        
        existencia = 0
        precio_final = 0 
        
        if rut_tienda:
            inv = Inventario.objects.filter(cod_barra_id=codigo, rut_tienda_id=rut_tienda).first()
            if inv:
                existencia = inv.stock_actual
                precio_final = inv.precio_venta

        datos_respuesta = {
            'codigo': producto.cod_barra,
            'descripcion': producto.descripcion,
            'marca': producto.marca,
            'categoria': producto.categoria,
            'precio_venta': precio_final,
            'stock_disponible': existencia
        }
        return JsonResponse(datos_respuesta, status=200)

    except Exception as error:
        print(f"🔥 ERROR SILENCIOSO EN CAJA (Lector): {error}")
        return JsonResponse({'error': f'Error en la búsqueda: {str(error)}'}, status=500)
    
@csrf_exempt
def registrar_venta(request):
    #carrito de compras divididas pagadas y fiadas
    #no mezcla dineros y ademas crea al cliente deudor
    if request.method == 'POST':
        try:
            datos = json.loads(request.body)
            rut_tienda_id = datos.get('rut_tienda')
            id_usuario_id = datos.get('id_usuario')
            carrito_pagado = datos.get('carrito_pagado', [])
            carrito_fiado = datos.get('carrito_fiado', [])
            cliente_datos = datos.get('cliente')
            total_bruto_pagado = datos.get('total_bruto_pagado', 0)

            if not carrito_pagado and not carrito_fiado:
                return JsonResponse({'error': 'No hay productos para procesar'}, status=400)

            with transaction.atomic():
                boletas_generadas = [] #DUDAS, PORQUE EXISTE BOLETAS_GENERADAS?? SI ES SISTEMA NO DA BOLETAS

                #1 PROCESAR PRODUCTOS PAGADOS
                # =============================================
                if carrito_pagado:
                    #calculo del monto para la caja
                    total_neto_pagado = int(round(total_bruto_pagado / 1.19))
                    iva_pagado = total_bruto_pagado - total_neto_pagado

                    venta_pagada = Venta.objects.create(
                        fecha_venta=timezone.now(),
                        total_neto=total_neto_pagado, 
                        iva=iva_pagado,
                        total_bruto=total_bruto_pagado,
                        estado_pago=True,
                        rut_tienda_id=rut_tienda_id, 
                        id_usuario_id=id_usuario_id  
                    )
                    boletas_generadas.append(venta_pagada.id_venta)

                    for item in carrito_pagado:
                        _procesar_descuento_inventario(item, venta_pagada, rut_tienda_id)

                #2 PROCESAR PRODUCTOS FIADOS
                # ==============================================
                if carrito_fiado and cliente_datos:
                    #1. busca al cliente o lo cre si es nuevo
                    cliente_obj, _ = ClienteFiado.objects.get_or_create(
                        rut=cliente_datos['rut'],
                        defaults={
                            'nombre': cliente_datos['nombre'],
                            'apellido': cliente_datos['apellido']
                        }
                    )

                    #2 calcula monto fiado para crear la deuda (venta con estado_pago=False)
                    total_bruto_fiado = sum(item['subtotal'] for item in carrito_fiado)
                    total_neto_fiado = int(round(total_bruto_fiado / 1.19))
                    iva_fiado = total_bruto_fiado - total_neto_fiado

                    # 3 genera registro de deuda
                    venta_fiada = Venta.objects.create(
                        fecha_venta=timezone.now(),
                        total_neto=total_neto_fiado, 
                        iva=iva_fiado,
                        total_bruto=total_bruto_fiado,
                        estado_pago=False, 
                        rut_tienda_id=rut_tienda_id, 
                        id_usuario_id=id_usuario_id,
                        rut_cliente_id=cliente_obj.rut 
                    )
                    boletas_generadas.append(venta_fiada.id_venta)

                    for item in carrito_fiado:
                        _procesar_descuento_inventario(item, venta_fiada, rut_tienda_id)

            return JsonResponse({
                'mensaje': 'Operación completada exitosamente', 
                'ids_ventas': boletas_generadas
            }, status=201)

        except Exception as error:
            return JsonResponse({'error': f'Rollback ejecutado: {str(error)}'}, status=500)
    else:
        return JsonResponse({'error': 'Método no permitido. Utilice POST.'}, status=405)


def _procesar_descuento_inventario(item, objeto_venta, rut_tienda_id):
    #egistrar el detalle y descuenta el stock
    #soporta prductos agranel y creación del inventario por tienda
    codigo_prod = item['codigo']
    cantidad_vendida = float(item['cantidad']) 
    precio_item = int(item.get('precio_venta', 0))

    #1 crea o recuera el producto
    producto_obj, _ = Producto.objects.get_or_create(
        cod_barra=codigo_prod,
        defaults={
            'descripcion': item.get('descripcion', 'Producto manual / No encontrado'),
            'volumen': 0,
            'marca': 'POR DEFINIR',
            'fabricante': 'POR DEFINIR',
            'categoria': 'POR DEFINIR'
        }
    )
    #2 detalle con precio inmutable
    DetalleVenta.objects.create(
        id_venta=objeto_venta,
        cod_barra=producto_obj,
        cantidad=cantidad_vendida,
        precio_unitario=precio_item
    )

    #3descuento del producto en la tienda
    inventario_obj, _ = Inventario.objects.get_or_create(
        cod_barra=producto_obj,
        rut_tienda_id=rut_tienda_id,
        defaults={
            'stock_actual': 0,
            'precio_venta': precio_item,
            'umbral_seguridad': None 
        }
    )
    
    stock_previo = inventario_obj.stock_actual #stock antes del descuento
    
    inventario_obj.stock_actual -= cantidad_vendida
    inventario_obj.save()

    # revisa si se activa el umbral de seguridad
    if inventario_obj.umbral_seguridad is not None:
        umbral = inventario_obj.umbral_seguridad
        
        # regla 1, el producto se acabo y esta vigilado por el umbral
        if stock_previo > 0 and inventario_obj.stock_actual <= 0:
            enviar_alerta_stock(inventario_obj, "QUIEBRE TOTAL DE STOCK")
            
        #regla 2, el producto cruzo el umbral
        elif stock_previo > umbral and inventario_obj.stock_actual <= umbral:
            enviar_alerta_stock(inventario_obj, "ALERTA DE UMBRAL CRÍTICO")

#valicion de la sesion del cajero
def pantalla_pos(request):
    id_usuario_actual = request.session.get('id_usuario')
    
    if not id_usuario_actual:
        return redirect('pantalla_login')

    #se revisa si tiene la sesion abierta de caja, si no la tiene lo redirige a abrir caja
    caja_activa = CajaSesion.objects.filter(id_usuario_id=id_usuario_actual, estado=True).exists()
    
    if not caja_activa:
        return redirect('pantalla_apertura_caja')

    return render(request, 'nucleo_sistema/pos.html')

def consultar_deuda_cliente(request):
    """
    Calcula el estado de cuenta exacto del cliente (Cuenta Corriente) aislado por SUCURSAL.
    Fórmula: Total Comprado (Local) - Total Abonado (Local) = Deuda Actual (Local).
    """
    rut_consulta = request.GET.get('rut')
    
    # 1. Recuperamos la tienda del administrador/cajero que está operando
    rut_tienda_actual = request.session.get('rut_tienda')
    
    if not rut_consulta:
        return JsonResponse({'error': 'RUT no proporcionado'}, status=400)

    if not rut_tienda_actual:
        return JsonResponse({'error': 'Sesión de tienda no válida. Inicie sesión nuevamente.'}, status=403)

    try:
        # La tabla de clientes es global, por lo que el nombre será el mismo en todas las tiendas (Esto es correcto, un RUT = una persona)
        cliente = ClienteFiado.objects.get(rut=rut_consulta)
        
        # 2. Sumar todas las boletas a nombre de este cliente, PERO SOLO de esta tienda
        suma_compras = Venta.objects.filter(
            rut_cliente=rut_consulta,
            rut_tienda=rut_tienda_actual # <--- Candado Analítico
        ).aggregate(Sum('total_bruto'))['total_bruto__sum'] or 0
        
        # 3. Sumar todos los abonos de dinero entregados, PERO SOLO en esta tienda
        suma_abonos = AbonoFiado.objects.filter(
            rut_cliente=rut_consulta,
            rut_tienda=rut_tienda_actual # <--- Candado Analítico
        ).aggregate(Sum('monto'))['monto__sum'] or 0
        
        # 4. Matemática pura
        deuda_actual = suma_compras - suma_abonos

        return JsonResponse({
            'rut': cliente.rut,
            'nombre_completo': f"{cliente.nombre} {cliente.apellido}",
            'total_historico_compras': suma_compras,
            'total_historico_pagos': suma_abonos,
            'deuda_actual': deuda_actual
        }, status=200)

    except ClienteFiado.DoesNotExist:
        return JsonResponse({'error': 'Cliente no encontrado en los registros de fiados.'}, status=404)
    
@csrf_exempt
def registrar_abono(request):
    """
    Recibe el dinero físico para cuadrar la caja de hoy y rebaja la deuda del cliente.
    """
    if request.method == 'POST':
        try:
            datos = json.loads(request.body)
            rut = datos.get('rut_cliente')
            monto_abono = int(datos.get('monto', 0))
            id_usuario_id = datos.get('id_usuario', 1) 
            
            if monto_abono <= 0:
                return JsonResponse({'error': 'El monto del abono debe ser mayor a cero.'}, status=400)

            with transaction.atomic():
                # 1. Ingresar el dinero a la tabla de recaudación (para la caja de hoy)
                nuevo_abono = AbonoFiado.objects.create(
                    fecha_pago=timezone.now(),
                    monto=monto_abono,
                    rut_cliente_id=rut,
                    id_usuario_id=id_usuario_id
                )

                # 2. GATILLO INTELIGENTE: Limpieza de Base de Datos
                suma_compras = Venta.objects.filter(rut_cliente=rut).aggregate(Sum('total_bruto'))['total_bruto__sum'] or 0
                suma_abonos = AbonoFiado.objects.filter(rut_cliente=rut).aggregate(Sum('monto'))['monto__sum'] or 0
                
                # Si con este último abono el cliente dejó su cuenta en cero (o a favor)
                if suma_abonos >= suma_compras:
                    # Marcamos todas sus boletas antiguas como pagadas para cerrar el ciclo
                    Venta.objects.filter(rut_cliente=rut, estado_pago=False).update(estado_pago=True)

            return JsonResponse({
                'mensaje': 'Abono registrado exitosamente. Caja cuadrada.',
                'id_abono': nuevo_abono.id_abono
            }, status=201)

        except Exception as error:
            return JsonResponse({'error': f'Error al registrar el abono: {str(error)}'}, status=500)
    else:
        return JsonResponse({'error': 'Método no permitido. Utilice POST.'}, status=405)

def pantalla_recaudacion(request):
    """
    Renderiza la interfaz gráfica del Módulo de Recaudación de Fiados.
    """
    return render(request, 'nucleo_sistema/recaudacion.html')

@csrf_exempt
def abrir_caja(request):
    """ Registra el inicio del turno asegurando la identidad por sesión del servidor. """
    if request.method == 'POST':
        datos = json.loads(request.body)
        
        # BARRERA ANALÍTICA: La API lee la memoria del servidor, no el payload del Frontend
        id_usuario_real = request.session.get('id_usuario')
        rut_tienda_real = request.session.get('rut_tienda')
        
        if not id_usuario_real:
            return JsonResponse({'error': 'Sesión expirada o inválida'}, status=403)
            
        sesion = CajaSesion.objects.create(
            id_usuario_id=id_usuario_real,
            rut_tienda_id=rut_tienda_real,
            monto_apertura=int(datos.get('monto_apertura', 0)),
            estado=True
        )
        return JsonResponse({'mensaje': 'Caja abierta exitosamente', 'id_sesion': sesion.id_sesion}, status=201)

def obtener_estado_cuadratura(request):
    """ 
    Calcula en tiempo real cuánto dinero físico debe haber en la caja.
    Fórmula: Fondo Inicial + Ventas Pagadas Hoy + Abonos de Fiados Hoy.
    """
    id_usuario = request.GET.get('id_usuario', 1)
    
    # Buscamos la sesión abierta para este usuario
    sesion = CajaSesion.objects.filter(id_usuario_id=id_usuario, estado=True).last()
    
    if not sesion:
        return JsonResponse({'error': 'No hay una sesión de caja abierta.'}, status=404)

    # 1. Ventas pagadas (Efectivo) desde que se abrió la caja
    ventas_hoy = Venta.objects.filter(
        id_usuario_id=id_usuario, 
        estado_pago=True, 
        fecha_venta__gte=sesion.fecha_apertura
    ).aggregate(Sum('total_bruto'))['total_bruto__sum'] or 0

    # 2. Abonos de fiados recibidos desde que se abrió la caja
    abonos_hoy = AbonoFiado.objects.filter(
        id_usuario_id=id_usuario, 
        fecha_pago__gte=sesion.fecha_apertura
    ).aggregate(Sum('monto'))['monto__sum'] or 0

    total_esperado = sesion.monto_apertura + ventas_hoy + abonos_hoy

    return JsonResponse({
        'id_sesion': sesion.id_sesion,
        'fecha_apertura': sesion.fecha_apertura,
        'fondo_inicial': sesion.monto_apertura,
        'ventas_efectivo': ventas_hoy,
        'abonos_fiados': abonos_hoy,
        'total_esperado_en_caja': total_esperado
    })

def pantalla_apertura_caja(request):
    return render(request, 'nucleo_sistema/apertura_caja.html')

def pantalla_cierre_caja(request):
    """ Renderiza la interfaz de cierre de turno. """
    return render(request, 'nucleo_sistema/cierre_caja.html')

@csrf_exempt
def registrar_cierre(request):
    """ Finaliza la sesión de caja y guarda la cuadratura. """
    if request.method == 'POST':
        datos = json.loads(request.body)
        id_usuario = datos.get('id_usuario')
        monto_real = int(datos.get('monto_real', 0))
        monto_esperado = int(datos.get('monto_esperado', 0))

        with transaction.atomic():
            sesion = CajaSesion.objects.filter(id_usuario_id=id_usuario, estado=True).last()
            if sesion:
                sesion.fecha_cierre = timezone.now()
                sesion.monto_cierre_real = monto_real
                sesion.monto_cierre_esperado = monto_esperado
                sesion.estado = False # Cierre de candado
                sesion.save()
                
                return JsonResponse({'mensaje': 'Caja cerrada exitosamente'}, status=200)
    
    return JsonResponse({'error': 'No se encontró sesión activa'}, status=404)

def cerrar_sesion(request):
    """ Destruye la sesión de Django y expulsa al usuario al login. """
    request.session.flush()
    return redirect('pantalla_login')

def pantalla_login(request):
    #acceso al sistema validando credenciales y redirige segun rol
    if request.method == 'POST':
        #strip() como medida preventiva contra espacio
        usuario_ingresado = request.POST.get('usuario', '').strip()
        clave_ingresada = request.POST.get('clave', '').strip()

        try:
            usuario_objeto = Usuario.objects.get(
                nombre_usuario=usuario_ingresado, 
                password=clave_ingresada
            )
            
            if not usuario_objeto.es_activo:
                messages.error(request, 'Acceso denegado: Esta cuenta ha sido desactivada.')
                return redirect('pantalla_login')
            
            if usuario_objeto.requiere_cambio_pass:
                # Guardamos el ID en una sesión temporal aislada
                request.session['usuario_en_cambio'] = usuario_objeto.id_usuario
                return render(request, 'nucleo_sistema/cambiar_password.html')
            
            # iyeccion de variables globales
            request.session['id_usuario'] = usuario_objeto.id_usuario
            request.session['rol'] = usuario_objeto.rol 

            rol_limpio = usuario_objeto.rol.strip().upper()
            
            print(f"1. ROL EXTRAÍDO DE BD: '{usuario_objeto.rol}'")
            print(f"2. ROL LIMPIO PARA COMPARAR: '{rol_limpio}'")
            
            #guarda trafico del rol
            if rol_limpio == 'ANALISTA':
                print("3. DECISIÓN: Yendo a Analista")
                return redirect('pantalla_consola_analista')
                
            elif rol_limpio == 'ADMINISTRADOR':
                print("3. DECISIÓN: Yendo a Dashboard Admin")
                request.session['rut_tienda'] = usuario_objeto.rut_tienda_id 
                return redirect('pantalla_dashboard')
                
            else:
                print("3. DECISIÓN: Cayó en el ELSE, yendo a Caja")
                request.session['rut_tienda'] = usuario_objeto.rut_tienda_id 
                return redirect('pantalla_apertura_caja')
            
        except Usuario.DoesNotExist:
            messages.error(request, 'Usuario o contraseña incorrectos')
            return redirect('pantalla_login')

    return render(request, 'nucleo_sistema/login.html')

def pantalla_dashboard(request):
    """
    Renderiza el Panel de Control del Administrador y calcula
    los KPIs y gráficos en tiempo real desde la base de datos.
    """
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if rol_sesion not in ['ADMINISTRADOR', 'ANALISTA']:
        return redirect('pantalla_apertura_caja')

    rut_tienda_actual = request.session.get('rut_tienda')
    hoy = timezone.now().date()
    
    # 1. Obtener datos geográficos
    nombre_t, comuna_t = "Almacén", "Sucursal"
    try:
        tienda_obj = Tienda.objects.get(rut_tienda=rut_tienda_actual)
        nombre_t = getattr(tienda_obj, 'nombre', "Almacén Central")
        comuna_t = getattr(tienda_obj, 'comuna', "Santiago")
    except:
        pass

    # 2. Calcular Ventas del Día y Fiados Activos
    ventas_hoy = Venta.objects.filter(
        rut_tienda=rut_tienda_actual, fecha_venta__date=hoy
    ).aggregate(total=Sum('total_bruto'))['total'] or 0

    total_fiado_historico = Venta.objects.filter(
        rut_tienda=rut_tienda_actual, estado_pago=False, rut_cliente__isnull=False
    ).aggregate(total=Sum('total_bruto'))['total'] or 0
    total_abonos = AbonoFiado.objects.aggregate(total=Sum('monto'))['total'] or 0
    deuda_viva = max(total_fiado_historico - total_abonos, 0)

    # ========================================================
    # 3. NUEVOS INDICADORES (Facturas e Inventario)
    # ========================================================
    
    # Contar facturas ingresadas exactamente en el mes y año actual
    facturas_del_mes = Factura.objects.filter(
        fecha_ingreso__year=hoy.year,
        fecha_ingreso__month=hoy.month
    ).count()

    # Multiplicar Stock Actual * Precio de Venta para toda la bodega
    valor_inventario = Inventario.objects.filter(
        rut_tienda=rut_tienda_actual, stock_actual__gt=0
    ).aggregate(
        valor_total=Sum(F('stock_actual') * F('precio_venta'))
    )['valor_total'] or 0

    # ========================================================
    # 4. GRÁFICOS (Barras y Dona)
    # ========================================================
    
    # A) Gráfico de Barras (Últimos 7 días)
    hace_7_dias = hoy - timedelta(days=6)
    ventas_semana = Venta.objects.filter(
        rut_tienda=rut_tienda_actual, fecha_venta__date__gte=hace_7_dias
    ).annotate(fecha_corta=TruncDate('fecha_venta')) \
     .values('fecha_corta').annotate(total=Sum('total_bruto')).order_by('fecha_corta')

    ventas_dict = {v['fecha_corta']: v['total'] for v in ventas_semana}
    etiquetas_dias, datos_ventas = [], []
    for i in range(6, -1, -1):
        dia_iter = hoy - timedelta(days=i)
        etiquetas_dias.append(dia_iter.strftime("%d/%m"))
        datos_ventas.append(ventas_dict.get(dia_iter, 0))

    # B) Gráfico de Dona (Top 5 Categorías por Valor de Venta)
    # Analítica: Cruzamos DetalleVenta con Producto para sumar el dinero real generado
    ventas_por_categoria = DetalleVenta.objects.filter(
        id_venta__rut_tienda=rut_tienda_actual
    ).values(
        nombre_cat=F('cod_barra__categoria')
    ).annotate(
        valor_total=Sum(F('cantidad') * F('precio_unitario'))
    ).order_by('-valor_total')[:5] # Extraemos las 5 mejores

    donut_labels = [item['nombre_cat'] for item in ventas_por_categoria]
    donut_data = [int(item['valor_total']) for item in ventas_por_categoria]

    # Salvavidas: Si no hay ventas aún
    if not donut_labels:
        donut_labels = ['Sin Ventas']
        donut_data = [0]

    # 5. Empaquetar y enviar
    contexto = {
        'nombre_tienda': nombre_t,
        'comuna_tienda': comuna_t,
        'kpi_ventas_dia': f"{ventas_hoy:,}".replace(',', '.'),
        'kpi_fiados_activos': f"{deuda_viva:,}".replace(',', '.'),
        'kpi_facturas_mes': facturas_del_mes, 
        'kpi_valor_inventario': f"{int(valor_inventario):,}".replace(',', '.'),
        
        # Variables JSON para inyectar en JavaScript (Frontend)
        'chart_labels': json.dumps(etiquetas_dias),
        'chart_data': json.dumps(datos_ventas),
        'donut_labels': json.dumps(donut_labels),
        'donut_data': json.dumps(donut_data)
    }

    return render(request, 'nucleo_sistema/dashboard_admin.html', contexto)

def pantalla_catalogo(request):
    """ Muestra el catálogo con filtros avanzados, paginación y proporciones optimizadas. """
    
    # 1. Barrera de Seguridad Normalizada
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if rol_sesion != 'ADMINISTRADOR':
        return redirect('pantalla_login')

    # 2. Captura de Parámetros de Búsqueda (GET)
    query_general = request.GET.get('q', '').strip()
    marca_filtro = request.GET.get('marca', '').strip()
    categoria_filtro = request.GET.get('categoria', '').strip()

    # 3. Consulta Base
    productos_query = Producto.objects.all().order_by('descripcion')

    # 4. Aplicación de Filtros Dinámicos
    if query_general:
        # Busca por coincidencia en descripción O en el código de barras
        productos_query = productos_query.filter(
            Q(descripcion__icontains=query_general) | Q(cod_barra__icontains=query_general)
        )
    if marca_filtro:
        productos_query = productos_query.filter(marca=marca_filtro)
    if categoria_filtro:
        productos_query = productos_query.filter(categoria=categoria_filtro)

    # 5. Paginación (Cargamos solo 50 productos por pantalla)
    paginador = Paginator(productos_query, 50)
    numero_pagina = request.GET.get('page')
    pagina_objetos = paginador.get_page(numero_pagina)

    # 6. Listas únicas para poblar los selectores desplegables
    marcas_unicas = Producto.objects.values_list('marca', flat=True).distinct().order_by('marca')
    categorias_unicas = Producto.objects.values_list('categoria', flat=True).distinct().order_by('categoria')

    # 7. Empaquetado del Contexto
    contexto = {
        'productos': pagina_objetos, # Pasamos el objeto paginado, no la lista completa
        'marcas': marcas_unicas,
        'categorias': categorias_unicas,
        # Devolvemos los filtros actuales para que no se borren de la pantalla al buscar
        'q_actual': query_general,
        'marca_actual': marca_filtro,
        'categoria_actual': categoria_filtro,
    }
    
    return render(request, 'nucleo_sistema/catalogo_productos.html', contexto)

def registrar_producto(request):
    """ Captura el POST del formulario y crea el registro en PostgreSQL. """
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if request.method == 'POST' and rol_sesion == 'ADMINISTRADOR':
        try:
            # Analítica: Extraemos limpiamente los datos del POST
            nuevo_producto = Producto(
                cod_barra=request.POST.get('cod_barra'),
                descripcion=request.POST.get('descripcion'),
                volumen=int(request.POST.get('volumen', 0)),
                marca=request.POST.get('marca'),
                fabricante=request.POST.get('fabricante'),
                categoria=request.POST.get('categoria'),
                precio_venta=int(request.POST.get('precio_venta', 0))
            )
            # Guardamos físicamente en la tabla 'producto'
            nuevo_producto.save()
            
        except Exception as e:
            # Aquí podríamos implementar un mensaje de error si el código de barras ya existe
            print(f"Error al guardar producto: {e}")

    # Redirigimos de vuelta a la misma pantalla para ver la tabla actualizada
    return redirect('pantalla_catalogo')

def pantalla_abastecimiento(request):
    """ Muestra la interfaz de ingreso de facturas. """
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if rol_sesion != 'ADMINISTRADOR':
        return redirect('pantalla_login')
    
    # Analítica: Ya no cargamos los productos aquí para no colapsar la RAM
    return render(request, 'nucleo_sistema/abastecimiento.html')

@csrf_exempt
def registrar_abastecimiento_api(request):
    """ 
    Procesa la factura y actualiza el stock físico. 
    Lógica: Si el producto no existe en el inventario de la tienda, lo crea. 
    Si existe, suma la cantidad recibida.
    """
    if request.method == 'POST':
        datos = json.loads(request.body)
        
        try:
            with transaction.atomic():
                # 1. Crear Cabecera de Factura
                factura_obj = Factura.objects.create(
                    folio_factura=datos['folio'],
                    es_compra_directa=datos['es_compra_directa'],
                    fecha_emision=datos['fecha_emision'],
                    fecha_ingreso=timezone.now().date()
                )

                for item in datos['items']:
                    # 2. Guardar Detalle de Factura
                    DetalleFactura.objects.create(
                        folio_factura=factura_obj,
                        cod_barra_id=item['codBarra'],
                        cantidad=item['cantidad'],
                        valor_compra=item['costo']
                    )

                    # 3. ACTUALIZACIÓN DE STOCK Y PRECIO DE VENTA
                    umbral_recibido = item.get('umbral_seguridad')
                    umbral_final = int(umbral_recibido) if umbral_recibido is not None else None

                    inventario_obj, creado = Inventario.objects.get_or_create(
                        cod_barra_id=item['codBarra'],
                        rut_tienda_id=datos['rut_tienda'],
                        defaults={
                            'stock_actual': 0, 
                            'precio_venta': int(item['precio_venta']), 
                            'umbral_seguridad': umbral_final
                        }
                    )
                    
                    inventario_obj.stock_actual += int(item['cantidad'])
                    inventario_obj.precio_venta = int(item['precio_venta'])
                    # Sobrescribimos el umbral con la nueva decisión del administrador
                    inventario_obj.umbral_seguridad = umbral_final
                    inventario_obj.save()

                return JsonResponse({'mensaje': 'Abastecimiento procesado'}, status=201)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)

    return JsonResponse({'error': 'Método no permitido'}, status=405)

def api_buscar_productos(request):
    """ Buscador predictivo, extrayendo el precio exclusivamente del inventario. """
    query = request.GET.get('q', '').strip()
    rut_tienda = request.GET.get('rut_tienda') or request.session.get('rut_tienda')
    
    if len(query) < 3:
        return JsonResponse([], safe=False)
    
    try:
        terminos = query.split()
        filtro_descripcion = Q()
        
        for termino in terminos:
            filtro_descripcion &= (Q(descripcion__icontains=termino) | Q(marca__icontains=termino))
        
        filtro_final = filtro_descripcion
        if query.isdigit():
            filtro_final |= Q(cod_barra__icontains=query)
            
        productos = Producto.objects.filter(filtro_final).order_by('descripcion')[:40]
        
        resultados = []
        for p in productos:
            # EL AJUSTE MATEMÁTICO: Nacen en 0
            precio_actual = 0
            existencia = 0
            
            if rut_tienda:
                inv = Inventario.objects.filter(cod_barra_id=p.cod_barra, rut_tienda_id=rut_tienda).first()
                if inv:
                    precio_actual = inv.precio_venta
                    existencia = inv.stock_actual

            resultados.append({
                'cod_barra': str(p.cod_barra),
                'descripcion': p.descripcion,
                'marca': p.marca,
                'precio_venta': precio_actual,
                'stock_disponible': existencia
            })
        
        return JsonResponse(resultados, safe=False)
        
    except Exception as error:
        print(f"🔥 ERROR SILENCIOSO EN CAJA (Teclado): {error}")
        return JsonResponse({'error': str(error)}, status=500)
    
def pantalla_ajustes(request):
    """ Muestra la pantalla de ajustes, filtra la tabla y alimenta al exportador. """
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if rol_sesion != 'ADMINISTRADOR':
        return redirect('pantalla_login')
        
    rut_tienda_actual = request.session.get('rut_tienda')
    
    # 1. Captura de Filtros (GET)
    query_general = request.GET.get('q', '').strip()
    marca_filtro = request.GET.get('marca', '').strip()
    categoria_filtro = request.GET.get('categoria', '').strip()

    # 2. Consulta Base
    inventario_tienda = Inventario.objects.filter(
        rut_tienda=rut_tienda_actual
    ).select_related('cod_barra')

    # 3. Aplicación de Filtros Matemáticos a la vista web
    if query_general:
        inventario_tienda = inventario_tienda.filter(
            Q(cod_barra__descripcion__icontains=query_general) | Q(cod_barra__cod_barra__icontains=query_general)
        )
    if marca_filtro:
        inventario_tienda = inventario_tienda.filter(cod_barra__marca=marca_filtro)
    if categoria_filtro:
        inventario_tienda = inventario_tienda.filter(cod_barra__categoria=categoria_filtro)

    inventario_tienda = inventario_tienda.order_by('cod_barra__descripcion')

    # 4. Extracción de Marcas y Categorías únicas para los desplegables
    marcas_unicas = Inventario.objects.filter(rut_tienda=rut_tienda_actual).values_list('cod_barra__marca', flat=True).distinct().order_by('cod_barra__marca')
    categorias_unicas = Inventario.objects.filter(rut_tienda=rut_tienda_actual).values_list('cod_barra__categoria', flat=True).distinct().order_by('cod_barra__categoria')

    # 5. Empaquetado
    contexto = {
        'inventario': inventario_tienda,
        'marcas': marcas_unicas,
        'categorias': categorias_unicas,
        'q_actual': query_general,
        'marca_actual': marca_filtro,
        'categoria_actual': categoria_filtro
    }

    return render(request, 'nucleo_sistema/ajustes_inventario.html', contexto)

@csrf_exempt
def registrar_ajuste_api(request):
    """ Sobrescribe el stock de un producto con el conteo físico real. """
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if request.method == 'POST' and rol_sesion == 'ADMINISTRADOR':
        import json
        datos = json.loads(request.body)
        rut_tienda_actual = request.session.get('rut_tienda')
        
        try:
            # Lógica: Busca el registro en la repisa de la tienda. 
            # Si el producto nunca había sido ingresado por factura, lo crea en 0 y luego lo ajusta.
            inv_obj, creado = Inventario.objects.get_or_create(
                cod_barra_id=datos['cod_barra'],
                rut_tienda_id=rut_tienda_actual,
                defaults={'stock_actual': 0}
            )
            
            # NOTA ANALÍTICA: En un ERP avanzado, aquí se guardaría el 'motivo' en una tabla 
            # de historial llamada "AuditoriaAjustes". Para tu estructura actual, procedemos 
            # directo a actualizar el valor final de la bodega.
            inv_obj.stock_actual = int(datos['nuevo_stock'])
            inv_obj.save()
            
            return JsonResponse({'mensaje': 'Ajuste procesado exitosamente'}, status=200)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)
            
    return JsonResponse({'error': 'Acceso denegado'}, status=403)

def pantalla_configuracion(request):
    """ Renderiza el módulo de Configuración de Sistema. """
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if rol_sesion != 'ADMINISTRADOR':
        return redirect('pantalla_login')
        
    # Analítica: Solo mostramos los usuarios que pertenecen a la tienda del Administrador actual
    rut_tienda_actual = request.session.get('rut_tienda')
    lista_usuarios = Usuario.objects.filter(rut_tienda_id=rut_tienda_actual).order_by('nombre')
    
    return render(request, 'nucleo_sistema/configuracion_sistema.html', {
        'usuarios': lista_usuarios
    })

def registrar_usuario(request):
    """ Procesa el formulario y crea un nuevo usuario con credencial autogenerada en la base de datos. """
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    
    if request.method == 'POST' and rol_sesion == 'ADMINISTRADOR':
        try:
            # 1. Capturamos los datos enviados por el administrador y la sesión
            nombre = request.POST.get('nombre', '').strip()
            primer_apellido = request.POST.get('primer_apellido', '').strip()
            segundo_apellido = request.POST.get('segundo_apellido', '').strip()
            rol = request.POST.get('rol', '').strip()
            mail = request.POST.get('mail', '').strip()
            password = request.POST.get('password', '').strip()
            rut_tienda_admin = str(request.session.get('rut_tienda', '')).strip()

            # 2. MOTOR ALGORÍTMICO: Generador de Usuario Único
            # Ejemplo: Si es "Carlos Pérez" en la tienda "776094468", genera "cperez_4468"
            if nombre and primer_apellido:
                base_usuario = f"{nombre[0].lower()}{primer_apellido.lower()}_{rut_tienda_admin[-4:]}".replace(" ", "")
            else:
                base_usuario = f"user_{rut_tienda_admin[-4:]}" # Respaldo de seguridad

            nombre_usuario_final = base_usuario
            contador = 1
            
            # Validación Matemática de unicidad (Evita la colisión en BD)
            while Usuario.objects.filter(nombre_usuario=nombre_usuario_final).exists():
                nombre_usuario_final = f"{base_usuario}{contador}"
                contador += 1

            # 3. Inyección Segura en Base de Datos
            nuevo_usuario = Usuario(
                nombre_usuario=nombre_usuario_final, # <--- Valor inyectado automáticamente
                nombre=nombre,
                primer_apellido=primer_apellido,
                segundo_apellido=segundo_apellido,
                rol=rol,
                mail=mail,
                password=password,
                es_activo=True, # Por defecto el usuario nace activo
                fecha_creacion=timezone.now(),
                rut_tienda_id=rut_tienda_admin # Se asigna a la misma tienda del admin creador
            )
            nuevo_usuario.save()
            
            # 4. Feedback Visual (UX)
            # Mandamos el aviso a la pantalla para que el admin sepa qué usuario se creó
            messages.success(request, f"Usuario registrado exitosamente. La credencial de acceso asignada es: {nombre_usuario_final}")
            
        except Exception as e:
            print(f"Error analítico al crear usuario: {e}")
            messages.error(request, "Error de sistema al intentar registrar el usuario.")

    return redirect('pantalla_configuracion')

@csrf_exempt
def api_reset_clave(request):
    """
    Recibe la orden del administrador y sobrescribe la contraseña
    de un usuario específico en la base de datos.
    """
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if request.method == 'POST' and rol_sesion == 'ADMINISTRADOR':
        datos = json.loads(request.body)
        rut_tienda_actual = request.session.get('rut_tienda')
        
        try:
            # Analítica: Candado de seguridad. Solo permite editar si el usuario es de su misma tienda.
            usuario_obj = Usuario.objects.get(
                id_usuario=datos['id_usuario'],
                rut_tienda_id=rut_tienda_actual
            )
            
            # Aplicamos la nueva clave
            usuario_obj.password = datos['nueva_clave']
            usuario_obj.save()
            
            return JsonResponse({'mensaje': 'Clave actualizada exitosamente'}, status=200)
            
        except Usuario.DoesNotExist:
            return JsonResponse({'error': 'Usuario no encontrado o no pertenece a esta sucursal'}, status=404)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)

    return JsonResponse({'error': 'Acceso denegado'}, status=403)

@csrf_exempt
def api_cambiar_estado(request):

    """ Activa o desactiva el acceso de un usuario al sistema. """
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if request.method == 'POST' and rol_sesion == 'ADMINISTRADOR':
        datos = json.loads(request.body)
        rut_tienda_actual = request.session.get('rut_tienda')
        admin_actual_id = request.session.get('id_usuario')
        
        try:
            # Candado 1: Evitar el auto-bloqueo
            if str(datos['id_usuario']) == str(admin_actual_id):
                return JsonResponse({'error': 'Operación denegada: No puede bloquear su propia cuenta de administrador.'}, status=400)

            # Candado 2: Verificar que el usuario pertenezca a la misma tienda
            usuario_obj = Usuario.objects.get(
                id_usuario=datos['id_usuario'],
                rut_tienda_id=rut_tienda_actual
            )
            
            # Invertimos el estado (Si es True pasa a False, y viceversa)
            usuario_obj.es_activo = not usuario_obj.es_activo
            usuario_obj.save()
            
            return JsonResponse({'mensaje': 'Estado actualizado exitosamente'}, status=200)
            
        except Usuario.DoesNotExist:
            return JsonResponse({'error': 'Usuario no encontrado.'}, status=404)
        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)

    return JsonResponse({'error': 'Acceso denegado'}, status=403)

def pantalla_recuperar_password(request):
    """ Muestra el formulario para ingresar el correo. """
    return render(request, 'nucleo_sistema/recuperar_password.html')

def procesar_recuperacion(request):
    """ Verifica el correo, valida el rol analíticamente y envía la clave temporal. """
    if request.method == 'POST':
        # Captura y limpieza de espacios
        email_ingresado = request.POST.get('mail', '').strip()
        
        try:
            # Búsqueda insensible a mayúsculas
            usuario_obj = Usuario.objects.get(mail__iexact=email_ingresado)
            rol_limpio = usuario_obj.rol.strip().upper()
            
            if rol_limpio in ['ADMINISTRADOR', 'ANALISTA']:
                # Generar clave temporal
                caracteres = string.ascii_letters + string.digits
                clave_temporal = ''.join(random.choice(caracteres) for i in range(8))
                
                # Guardar en base de datos
                usuario_obj.password = clave_temporal
                usuario_obj.requiere_cambio_pass = True
                usuario_obj.save()
                
                # Envío del correo
                send_mail(
                    'Recuperación de Contraseña - Market Data',
                    f'Hola {usuario_obj.nombre}, tu clave temporal de acceso es: {clave_temporal}. '
                    f'Por favor, cámbiala al ingresar.',
                    'soporte@marketdata.cl',
                    [usuario_obj.mail],
                    fail_silently=False,
                )
                
                # --- INYECCIÓN EXACTA DE TUS LÍNEAS AQUÍ ---
                messages.success(request, 'Éxito: Se ha enviado una clave temporal a tu correo. Por favor, revisa tu bandeja de entrada o spam.')
                return redirect('pantalla_login')
                # -------------------------------------------
                
            else:
                return render(request, 'nucleo_sistema/recuperar_password.html', {
                    'error': 'El correo no coincide con un Administrador o Analista activo.'
                })
                
        except Usuario.DoesNotExist:
            return render(request, 'nucleo_sistema/recuperar_password.html', {
                'error': 'El correo no coincide con un Administrador o Analista activo.'
            })
            
    return redirect('pantalla_login')

def pantalla_reportes(request):
    """
    Módulo 5: BI con cruce de tablas dinámico.
    Calcula la ganancia buscando el valor_compra en las facturas.
    """
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if rol_sesion not in ['ADMINISTRADOR', 'ANALISTA']:
        return redirect('pantalla_login')

    rut_tienda_actual = request.session.get('rut_tienda')

    # 1. CÁLCULO DE GANANCIA (Cruce con DetalleFactura)
    detalles = DetalleVenta.objects.filter(id_venta__rut_tienda=rut_tienda_actual)
    
    ganancia_total = 0
    for item in detalles:
        # Buscamos el último precio de compra registrado para este producto
        factura_info = DetalleFactura.objects.filter(cod_barra=item.cod_barra).last()
        costo = factura_info.valor_compra if factura_info else 0
        
        # Ganancia = (Precio Venta - Costo Adquisición) * Cantidad
        ganancia_total += (item.precio_unitario - costo) * item.cantidad

    # 2. PROMEDIO DE PRODUCTOS POR VENTA Y MÉTRICAS GENERALES
    total_transacciones = Venta.objects.filter(rut_tienda=rut_tienda_actual).count()
    total_items = detalles.aggregate(total=Sum('cantidad'))['total'] or 0
    promedio_productos = round(total_items / total_transacciones, 1) if total_transacciones > 0 else 0

    total_ingresos = Venta.objects.filter(
        rut_tienda=rut_tienda_actual
    ).aggregate(total=Sum('total_bruto'))['total'] or 0

    # 3. RANKING TOP 10 PRODUCTOS (Variables sincronizadas con tu HTML actual)
    ranking_productos = detalles.values(
        nombre=F('cod_barra__descripcion')
    ).annotate(
        unidades_vendidas=Sum('cantidad'),
        total_recaudado=Sum(F('cantidad') * F('precio_unitario'))
    ).order_by('-unidades_vendidas')[:10]

    # 4. TOP 10 PRODUCTOS CRÍTICOS (CEROS ARRIBA)
    productos_criticos = Inventario.objects.filter(
        rut_tienda=rut_tienda_actual
    ).select_related('cod_barra').order_by('stock_actual')[:10]

    # 5. EMPAQUETADO EXACTO
    contexto = {
        'total_ingresos': f"{total_ingresos:,}".replace(',', '.'),
        'total_transacciones': total_transacciones,
        'ganancia_total': f"{int(ganancia_total):,}".replace(',', '.'),
        'promedio_productos': promedio_productos,
        
        # Las variables que tus tablas están esperando
        'ranking_productos': ranking_productos,
        'productos_criticos': productos_criticos,
    }

    return render(request, 'nucleo_sistema/reportes_analitica.html', contexto)

def pantalla_consola_analista(request):
    """
    Consola Global: Filtros persistentes y sincronización de CSV.
    """
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if rol_sesion != 'ANALISTA':
        return redirect('pantalla_login')

    tiendas = Tienda.objects.all().order_by('nombre')
    regiones_disponibles = Comuna.objects.values_list('region', flat=True).distinct().order_by('region')
    comunas_disponibles = Comuna.objects.all().order_by('nombre_comuna')

    # 1. CAPTURA DE FILTROS
    regiones_filtro = request.GET.getlist('regiones')
    comunas_filtro = request.GET.getlist('comunas')
    tiendas_filtro = request.GET.getlist('tiendas')
    fecha_inicio = request.GET.get('fecha_inicio', '')
    fecha_fin = request.GET.get('fecha_fin', '')
    
    # 2. FILTRADO DINÁMICO EN CASCADA
    tiendas_filtradas = tiendas
    if comunas_filtro:
        tiendas_filtradas = tiendas_filtradas.filter(id_comuna__in=comunas_filtro)
    elif regiones_filtro:
        comunas_ids = Comuna.objects.filter(region__in=regiones_filtro).values_list('id_comuna', flat=True)
        tiendas_filtradas = tiendas_filtradas.filter(id_comuna__in=comunas_ids)

    if tiendas_filtro:
        tiendas_filtradas = tiendas_filtradas.filter(rut_tienda__in=tiendas_filtro)
    
    lista_ruts = tiendas_filtradas.values_list('rut_tienda', flat=True)

    # 3. CONSTRUCCIÓN DE LA CONSULTA (Blindada contra Zonas Horarias)
    ventas_query = Venta.objects.filter(rut_tienda__in=lista_ruts)
    
    # EL AJUSTE MATEMÁTICO: Forzamos la hora exacta esquivando el error de __date
    if fecha_inicio:
        ventas_query = ventas_query.filter(fecha_venta__gte=f"{fecha_inicio} 00:00:00")
    if fecha_fin:
        ventas_query = ventas_query.filter(fecha_venta__lte=f"{fecha_fin} 23:59:59")

    # 4. INTERCEPTOR CSV (Reporte de Auditoría y Rentabilidad)
    if request.GET.get('exportar') == 'csv':
        import csv
        from django.http import HttpResponse
        
        response = HttpResponse(content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="Auditoria_BI_{fecha_inicio}_al_{fecha_fin}.csv"'
        response.write(u'\ufeff'.encode('utf8')) # Soporte para Excel y acentos
        
        writer = csv.writer(response, delimiter=';')
        
        # ORDEN LÓGICO: Ubicación > Tiempo > Producto > Operación > Finanzas
        writer.writerow([
            'Región', 'Comuna', 'Tienda', 'Tipo Local', 
            'Fecha', 'Hora', 'Estado', 
            'Cód. Barra', 'Producto', 'Marca', 'Fabricante', 
            'Cantidad', 'Stock Disponible', 
            'Precio Venta ($)', 'Costo Compra ($)', 'Margen Unitario ($)'
        ])

        # Consulta optimizada con select_related
        detalles = DetalleVenta.objects.filter(id_venta__in=ventas_query).select_related('id_venta', 'cod_barra', 'id_venta__rut_tienda')
        
        # --- MOTOR DE PRE-PROCESAMIENTO (Optimización de Memoria) ---
        codigos_presentes = detalles.values_list('cod_barra', flat=True).distinct()
        
        # 1. Mapa de Costos (Último precio de compra)
        costos_historicos = DetalleFactura.objects.filter(cod_barra_id__in=codigos_presentes).order_by('id_detalle_factura')
        dict_costos = {f.cod_barra_id: f.valor_compra for f in costos_historicos}

        # 2. Mapa de Inventario (Stock actual en la tienda correspondiente)
        inventario_data = Inventario.objects.filter(rut_tienda__in=lista_ruts).values('cod_barra_id', 'rut_tienda_id', 'stock_actual')
        dict_stock = {(i['cod_barra_id'], i['rut_tienda_id']): i['stock_actual'] for i in inventario_data}

        for d in detalles:
            v = d.id_venta
            t = v.rut_tienda
            p = d.cod_barra
            comuna_obj = Comuna.objects.filter(id_comuna=t.id_comuna).first()
            
            # Lógica de campos solicitada
            estado_texto = "PAGADO" if v.estado_pago else "FIADO"
            tipo_l = t.tipo_tienda if t.tipo_tienda else "N/A"
            
            # Recuperación de mapas (O(1) Performance)
            costo_u = dict_costos.get(p.cod_barra, 0)
            stock_disp = dict_stock.get((p.cod_barra, t.rut_tienda), 0)
            margen_u = d.precio_unitario - costo_u
            
            writer.writerow([
                comuna_obj.region if comuna_obj else 'N/A',
                comuna_obj.nombre_comuna if comuna_obj else 'N/A',
                t.nombre,
                tipo_l,
                v.fecha_venta.strftime('%d/%m/%Y'),
                v.fecha_venta.strftime('%H:%M:%S'),
                estado_texto,
                p.cod_barra,
                p.descripcion,
                p.marca,
                p.fabricante,
                d.cantidad,
                stock_disp,
                d.precio_unitario,
                costo_u,
                margen_u
            ])
            
        return response
    
    # 5. DASHBOARD VISUAL (Carga de métricas y Gráficos)
    from collections import defaultdict
    import json
    
    total_bruto = ventas_query.aggregate(total=Sum('total_bruto'))['total'] or 0
    
    # A) Gráfico de Barras: Fabricantes
    ventas_fabricante = DetalleVenta.objects.filter(id_venta__in=ventas_query).values(
        nombre_fabricante=F('cod_barra__fabricante')
    ).annotate(
        total_ventas=Sum(F('cantidad') * F('precio_unitario'))
    ).order_by('-total_ventas')[:10]

    labels_barras = [v['nombre_fabricante'] for v in ventas_fabricante]
    datos_barras = [int(v['total_ventas']) for v in ventas_fabricante]

    # B) Gráfico de Dona: Abastecimiento
    facturas_query = Factura.objects.all()
    if fecha_inicio: facturas_query = facturas_query.filter(fecha_ingreso__gte=fecha_inicio)
    if fecha_fin: facturas_query = facturas_query.filter(fecha_ingreso__lte=fecha_fin)
    
    datos_dona = [
        facturas_query.filter(es_compra_directa=True).count(),
        facturas_query.filter(es_compra_directa=False).count()
    ]

    # C) Gráfico Multi-Línea por TIPO DE TIENDA (Procesamiento en RAM)
    ventas_brutas = ventas_query.values('rut_tienda__tipo_tienda', 'fecha_venta', 'total_bruto')
    
    fechas_set = set()
    tipos_data = defaultdict(dict)
    
    for v in ventas_brutas:
        tipo = v['rut_tienda__tipo_tienda'] if v['rut_tienda__tipo_tienda'] else "SIN CATEGORÍA"
        
        # Extraemos la fecha pura con Python, esquivando el error del motor SQL
        fecha_obj = v['fecha_venta'].date()
        fechas_set.add(fecha_obj)
        
        if fecha_obj in tipos_data[tipo]:
            tipos_data[tipo][fecha_obj] += int(v['total_bruto'])
        else:
            tipos_data[tipo][fecha_obj] = int(v['total_bruto'])

    fechas_ordenadas = sorted(list(fechas_set))
    labels_multilinea = [f.strftime("%d/%m") for f in fechas_ordenadas]
    
    datasets_multilinea = []
    colores_vivos = ['#007bff', '#28a745', '#dc3545', '#ffc107', '#17a2b8', '#6610f2']
    c_idx = 0
    
    for tipo_tienda, datos_fechas in tipos_data.items():
        data_array = [datos_fechas.get(f, 0) for f in fechas_ordenadas]
        datasets_multilinea.append({
            'label': tipo_tienda,
            'data': data_array,
            'borderColor': colores_vivos[c_idx % len(colores_vivos)],
            'backgroundColor': colores_vivos[c_idx % len(colores_vivos)],
            'borderWidth': 3,
            'fill': False,
            'tension': 0.3
        })
        c_idx += 1
    
    # D) Gráfico de Barras Agrupadas: Marcas por Tipo de Tienda (En unidades)
    # 1. Obtenemos el Top 10 de marcas a nivel global para no saturar el gráfico
    top_marcas_qs = DetalleVenta.objects.filter(id_venta__in=ventas_query).values(
        'cod_barra__marca'
    ).annotate(total_unidades=Sum('cantidad')).order_by('-total_unidades')[:10]
    
    lista_top_marcas = [m['cod_barra__marca'] for m in top_marcas_qs if m['cod_barra__marca']]

    # 2. Extraemos el detalle solo de esas marcas ganadoras
    ventas_marcas = DetalleVenta.objects.filter(
        id_venta__in=ventas_query,
        cod_barra__marca__in=lista_top_marcas
    ).values(
        'id_venta__rut_tienda__tipo_tienda', 'cod_barra__marca'
    ).annotate(unidades=Sum('cantidad'))

    tipos_data_marcas = defaultdict(lambda: defaultdict(int))
    for v in ventas_marcas:
        tipo = v['id_venta__rut_tienda__tipo_tienda'] if v['id_venta__rut_tienda__tipo_tienda'] else "SIN CATEGORÍA"
        marca = v['cod_barra__marca']
        tipos_data_marcas[tipo][marca] += v['unidades']

    datasets_marcas = []
    c_idx2 = 0
    colores_marcas = ['#fd7e14', '#20c997', '#e83e8c', '#6f42c1', '#17a2b8', '#343a40']
    
    for tipo, marcas_dict in tipos_data_marcas.items():
        data_array = [marcas_dict.get(m, 0) for m in lista_top_marcas]
        datasets_marcas.append({
            'label': tipo,
            'data': data_array,
            'backgroundColor': colores_marcas[c_idx2 % len(colores_marcas)],
            'borderWidth': 0
        })
        c_idx2 += 1

    # E) Analítica de Horarios (Picos y Valles) - Cálculo blindado
    ventas_fechas_crudas = ventas_query.values_list('fecha_venta', flat=True)
    dict_horas = {i: 0 for i in range(24)}
    total_transacciones = ventas_fechas_crudas.count()

    from django.utils import timezone

    for fecha_obj in ventas_fechas_crudas:
        # BARRERA ANALÍTICA: Validamos si la fecha trae zona horaria (Aware) o viene limpia (Naive)
        if timezone.is_aware(fecha_obj):
            hora_local = timezone.localtime(fecha_obj).hour
        else:
            # Si viene sin zona horaria, asumimos que ya está en la hora correcta del servidor
            hora_local = fecha_obj.hour
            
        dict_horas[hora_local] += 1
        
    tabla_horas = []
    # Generamos la tabla solo para las horas que tuvieron movimiento
    for hora in range(24):
        cant = dict_horas[hora]
        if cant > 0:
            porcentaje = round((cant / total_transacciones) * 100, 1)
            hora_formato = f"{str(hora).zfill(2)}:00 - {str(hora).zfill(2)}:59"
            
            # Etiquetado algorítmico: >= 12% es Pico (Rojo), <= 3% es Valle (Gris)
            estado = "normal"
            if porcentaje >= 12: estado = "pico"
            elif porcentaje <= 3: estado = "valle"
            
            tabla_horas.append({
                'rango': hora_formato,
                'cantidad': cant,
                'porcentaje': porcentaje,
                'estado': estado
            })
    # F) Gráfico de Barras Horizontales: Top Categorías por Ingresos
    ventas_categoria = DetalleVenta.objects.filter(id_venta__in=ventas_query).values(
        nombre_categoria=F('cod_barra__categoria')
    ).annotate(
        total_recaudado=Sum(F('cantidad') * F('precio_unitario'))
    ).order_by('-total_recaudado')[:7] # Top 7 para que encaje estéticamente

    labels_categorias = [c['nombre_categoria'] if c['nombre_categoria'] else "SIN CATEGORÍA" for c in ventas_categoria]
    datos_categorias = [int(c['total_recaudado']) for c in ventas_categoria]

    contexto = {
        'tiendas': tiendas,
        'regiones': regiones_disponibles,
        'comunas': comunas_disponibles,
        'total_ingresos': f"{total_bruto:,}".replace(',', '.'),
        'labels_barras': json.dumps(labels_barras),
        'datos_barras': json.dumps(datos_barras),
        'labels_dona': json.dumps(['Compra Directa', 'Proveedor Mayorista']),
        'datos_dona': json.dumps(datos_dona),
        'labels_multilinea': json.dumps(labels_multilinea),
        'datasets_multilinea': json.dumps(datasets_multilinea),        
        'fecha_inicio': fecha_inicio, 
        'fecha_fin': fecha_fin,
        'tiendas_seleccionadas': tiendas_filtro,
        'regiones_seleccionadas': regiones_filtro,
        'comunas_seleccionadas': comunas_filtro,
        'labels_marcas': json.dumps(lista_top_marcas),
        'datasets_marcas': json.dumps(datasets_marcas),
        'tabla_horas': tabla_horas,
        'labels_categorias': json.dumps(labels_categorias),
        'datos_categorias': json.dumps(datos_categorias),
    }
    return render(request, 'nucleo_sistema/dashboard_analista.html', contexto)

def exportar_inventario_excel(request):
    # 1. Validación de seguridad
    rol_sesion = str(request.session.get('rol', '')).strip().upper()
    if rol_sesion != 'ADMINISTRADOR':
        return redirect('pantalla_login')

    rut_tienda_actual = request.session.get('rut_tienda')
    
    # 2. CAPTURA DE FILTROS (Sincronización con la interfaz)
    q = request.GET.get('q', '').strip()
    marca = request.GET.get('marca', '').strip()
    categoria = request.GET.get('categoria', '').strip()

    # 3. Consulta Base
    inventario_query = Inventario.objects.filter(
        rut_tienda=rut_tienda_actual
    ).select_related('cod_barra')

    # 4. APLICACIÓN DE LOS MISMOS FILTROS QUE EN PANTALLA
    if q:
        inventario_query = inventario_query.filter(
            Q(cod_barra__descripcion__icontains=q) | Q(cod_barra__cod_barra__icontains=q)
        )
    if marca:
        inventario_query = inventario_query.filter(cod_barra__marca=marca)
    if categoria:
        inventario_query = inventario_query.filter(cod_barra__categoria=categoria)

    inventario_query = inventario_query.order_by('cod_barra__descripcion')

    # 5. Generación del CSV (Configuración Excel)
    response = HttpResponse(content_type='text/csv')
    fecha_str = timezone.now().strftime("%d-%m-%Y")
    response['Content-Disposition'] = f'attachment; filename="Stock_Filtrado_{fecha_str}.csv"'
    response.write(u'\ufeff'.encode('utf8')) # Soporte para acentos
    
    writer = csv.writer(response, delimiter=';')
    writer.writerow(['Cód. Barra', 'Descripción', 'Marca', 'Categoría', 'Stock Actual', 'Precio Venta ($)'])

    for item in inventario_query:
        writer.writerow([
            item.cod_barra.cod_barra,
            item.cod_barra.descripcion,
            item.cod_barra.marca,
            item.cod_barra.categoria,
            item.stock_actual,
            item.precio_venta
        ])

    return response

def enviar_alerta_stock(inventario_obj, tipo_alerta):
    """ Busca a los administradores de la sucursal y despacha la alerta de inventario. """
    try:
        # Extraemos a todos los administradores activos de ESA tienda en particular
        admins = Usuario.objects.filter(
            rut_tienda_id=inventario_obj.rut_tienda_id, 
            rol__iexact='ADMINISTRADOR', 
            es_activo=True
        )
        correos_destino = [admin.mail for admin in admins if admin.mail]
        
        if correos_destino:
            producto = inventario_obj.cod_barra.descripcion
            stock = inventario_obj.stock_actual
            umbral = inventario_obj.umbral_seguridad
            
            asunto = f"⚠️ {tipo_alerta}: {producto}"
            mensaje = (
                f"Estimado Administrador,\n\n"
                f"El sistema ha detectado una alerta de inventario en su sucursal:\n\n"
                f"- Producto: {producto}\n"
                f"- Stock Actual: {stock} unidades\n"
                f"- Umbral de Seguridad: {umbral} unidades\n\n"
                f"Por favor, gestione el abastecimiento a la brevedad."
            )
            
            send_mail(asunto, mensaje, 'soporte@marketdata.cl', correos_destino, fail_silently=True)
    except Exception as e:
        print(f"🔥 Error al despachar alerta de correo: {e}")

def api_buscar_cliente(request):
    """ Busca un cliente por RUT y devuelve sus datos básicos en JSON. """
    rut_buscado = request.GET.get('rut', '').strip()
    
    if not rut_buscado:
        return JsonResponse({'existe': False}, status=400)
        
    try:
        cliente = ClienteFiado.objects.get(rut=rut_buscado)
        return JsonResponse({
            'existe': True,
            'nombre': cliente.nombre,
            'apellido': cliente.apellido
        })
    except ClienteFiado.DoesNotExist:
        return JsonResponse({'existe': False})

def procesar_cambio_password(request):
    """ Valida y actualiza la clave obligatoria del usuario """
    if request.method == 'POST':
        # Validar que el usuario venga del flujo correcto
        id_temp = request.session.get('usuario_en_cambio')
        if not id_temp:
            return redirect('pantalla_login')

        nueva_clave = request.POST.get('nueva_clave', '').strip()
        confirmar_clave = request.POST.get('confirmar_clave', '').strip()

        # Validación algorítmica
        if nueva_clave != confirmar_clave:
            messages.error(request, 'Las contraseñas no coinciden. Inténtalo de nuevo.')
            return render(request, 'nucleo_sistema/cambiar_password.html')

        try:
            # Impactar la base de datos
            usuario = Usuario.objects.get(id_usuario=id_temp)
            usuario.password = nueva_clave
            usuario.requiere_cambio_pass = False # Apagamos la alarma
            usuario.save()

            # Destruir la sesión temporal por seguridad
            del request.session['usuario_en_cambio']

            # Redirigir al inicio con éxito
            messages.success(request, 'Clave actualizada correctamente. Ahora puedes iniciar sesión.')
            return redirect('pantalla_login')

        except Usuario.DoesNotExist:
            return redirect('pantalla_login')

    return redirect('pantalla_login')