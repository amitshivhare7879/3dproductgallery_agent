# Add these alongside your existing product API routes in urls.py

path('api/products/get/<int:product_id>', api.get_product_api, name='api-product-get-no-slash'),
path('api/products/get/<int:product_id>/', api.get_product_api, name='api-product-get'),
path('api/products/delete/<int:product_id>', api.delete_product_api, name='api-product-delete-no-slash'),
path('api/products/delete/<int:product_id>/', api.delete_product_api, name='api-product-delete'),
path('api/products/list', api.list_products_api, name='api-product-list-no-slash'),
path('api/products/list/', api.list_products_api, name='api-product-list'),
