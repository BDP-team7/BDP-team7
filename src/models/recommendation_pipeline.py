import os

import pandas as pd
from pyspark.ml import Pipeline
from pyspark.ml.classification import RandomForestClassifier
from pyspark.ml.evaluation import MulticlassClassificationEvaluator
from pyspark.ml.feature import (CountVectorizer, OneHotEncoder, StringIndexer,
                                VectorAssembler)
from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.functions import (col, collect_list, collect_set, desc,
                                   explode, expr, lower, regexp_replace,
                                   row_number, size, split, trim, udf, when)
from pyspark.sql.window import Window


class ProductRecommendation:
    def __init__(self, product_rankings_path, product_keywords_path, output_dir):
        self.spark = SparkSession.builder.appName("BDP").getOrCreate()
        self.product_rankings = self.spark.read.csv(product_rankings_path, header=True, inferSchema=True)
        self.product_keywords = self.spark.read.csv(product_keywords_path, header=True, inferSchema=True)
        self.output_dir = output_dir

    def preprocess_data(self):
        # productId를 기준으로 date 내림차순 정렬
        window_spec = Window.partitionBy("productId").orderBy(desc("date"))
        
        # 각 productId 그룹에서 가장 최신 date인 행을 선택
        self.product_rankings = (
            self.product_rankings.withColumn("row_number", row_number().over(window_spec))
                                .filter(col("row_number") == 1)  # row_number가 1인 행만 선택
                                .drop("row_number")  # 임시 컬럼 제거
        )
        
        # 상위 50%인 150위를 기준으로 분리
        self.product_rankings = self.product_rankings.withColumn(
            "recommend",
            when(col("ranking") <= 150, 1).otherwise(0)
        )

        # ranking 컬럼 삭제
        self.product_rankings = self.product_rankings.drop("ranking")
        
        # DataFrame 조인
        self.joined_data = self.product_rankings.join(self.product_keywords, on="productId", how="inner")

        # 결측값 처리
        self.joined_data = self.joined_data.fillna({"colors": "unknown", "keywords": "", "rating": 0, "ratingCount": 0})

        # 범주형 데이터 인코딩
        brand_indexer = StringIndexer(inputCol="brandName", outputCol="brand_index")
        colors_indexer = StringIndexer(inputCol="colors", outputCol="colors_index")
        brand_encoder = OneHotEncoder(inputCol="brand_index", outputCol="brand_ohe")
        colors_encoder = OneHotEncoder(inputCol="colors_index", outputCol="colors_ohe")

        # 인코더 모델 피팅 및 변환
        pipeline = Pipeline(stages=[brand_indexer, colors_indexer, brand_encoder, colors_encoder])
        self.joined_data = pipeline.fit(self.joined_data).transform(self.joined_data)

        # keywords 열을 배열로 변환
        self.joined_data = self.joined_data.withColumn("keywords", F.split(F.col("keywords"), ", "))

    def vectorize_keywords(self):
        # Keywords 열을 단어로 분리
        keywords_exploded = self.joined_data.withColumn("keyword", explode(col("keywords")))
        
        # 모든 단어를 소문자로 변환하고 특수문자 제거
        keywords_cleaned = keywords_exploded.withColumn(
            "keyword",
            regexp_replace(lower(col("keyword")), "[^가-힣a-zA-Z]", "")
        )
        
        # 키워드 등장 횟수 계산
        top_keywords_df = (
            keywords_cleaned.groupBy("keyword")
            .count()
            .orderBy(col("count").desc())
            .limit(100)  # 상위 100개의 키워드 추출
        )

        top_keywords_list = [row["keyword"] for row in top_keywords_df.collect()]
        
        # 상위 100개의 키워드만 포함한 새 컬럼 생성
        top_keywords_list = [f'"{kw}"' for kw in top_keywords_list]
        keywords_expr = ', '.join(top_keywords_list)
        filtered_keywords = self.joined_data.withColumn(
            "filtered_keywords",
            F.expr(f"filter(keywords, x -> array_contains(array({keywords_expr}), x))")
        )

        # CountVectorizer를 사용하여 키워드 벡터화
        vectorizer = CountVectorizer(inputCol="filtered_keywords", outputCol="keyword_features")
        vectorized_model = vectorizer.fit(filtered_keywords)
        self.vectorized_data = vectorized_model.transform(filtered_keywords)

    def train_models(self):
        def train_model(data):
            assembler = VectorAssembler(
                inputCols=["brand_ohe", "colors_ohe", "keyword_features", "price", "discountRate", "conversionRate",
                           "trending", "totalSales", "views", "likes", "rating", "ratingCount"],
                outputCol="features"
            )
            rf = RandomForestClassifier(featuresCol="features", labelCol="recommend")
            pipeline = Pipeline(stages=[assembler, rf])
            model = pipeline.fit(data)
            return model

        self.clothes_top_data = self.vectorized_data.filter(col("category") == "clothes_top")
        self.pants_data = self.vectorized_data.filter(col("category") == "pants")
        self.shoes_data = self.vectorized_data.filter(col("category") == "shoes")
        self.outers_data = self.vectorized_data.filter(col("category") == "outers")

        self.clothes_top_data_train, self.clothes_top_data_test = self.clothes_top_data.randomSplit([0.7, 0.3], seed=42)
        self.pants_data_train, self.pants_data_test = self.pants_data.randomSplit([0.7, 0.3], seed=42)
        self.shoes_data_train, self.shoes_data_test = self.shoes_data.randomSplit([0.7, 0.3], seed=42)
        self.outers_data_train, self.outers_data_test = self.outers_data.randomSplit([0.7, 0.3], seed=42)

        # 모델 훈련
        self.clothes_top_model = train_model(self.clothes_top_data_train)
        self.pants_model = train_model(self.pants_data_train)
        self.shoes_model = train_model(self.shoes_data_train)
        self.outers_model = train_model(self.outers_data_train)

    def evaluate_models(self):
        def evaluate_model(model, test_data):
            predictions = model.transform(test_data)
            #evaluator = MulticlassClassificationEvaluator(labelCol="recommend", predictionCol="prediction", metricName="accuracy")
            #accuracy = evaluator.evaluate(predictions)

            evaluator_accuracy = MulticlassClassificationEvaluator(labelCol="recommend", predictionCol="prediction", metricName="accuracy")
            evaluator_precision = MulticlassClassificationEvaluator(labelCol="recommend", predictionCol="prediction", metricName="weightedPrecision")
            evaluator_recall = MulticlassClassificationEvaluator(labelCol="recommend", predictionCol="prediction", metricName="weightedRecall")
        
            accuracy = evaluator_accuracy.evaluate(predictions)
            precision = evaluator_precision.evaluate(predictions)
            recall = evaluator_recall.evaluate(predictions)
            return accuracy, precision, recall

        # 훈련 데이터 정확도, precision, recall 계산
        """self.clothes_top_accuracy_train = evaluate_model(self.clothes_top_model, self.clothes_top_data_train)
        self.pants_accuracy_train = evaluate_model(self.pants_model, self.pants_data_train)
        self.shoes_accuracy_train = evaluate_model(self.shoes_model, self.shoes_data_train)
        self.outers_accuracy_train = evaluate_model(self.outers_model, self.outers_data_train)"""
        self.clothes_top_accuracy_train, self.clothes_top_precision_train, self.clothes_top_recall_train = evaluate_model(self.clothes_top_model, self.clothes_top_data_train)
        self.pants_accuracy_train, self.pants_precision_train, self.pants_recall_train = evaluate_model(self.pants_model, self.pants_data_train)
        self.shoes_accuracy_train, self.shoes_precision_train, self.shoes_recall_train = evaluate_model(self.shoes_model, self.shoes_data_train)
        self.outers_accuracy_train, self.outers_precision_train, self.outers_recall_train = evaluate_model(self.outers_model, self.outers_data_train)


        # 테스트 데이터 정확도, precision, recall 계산
        """self.clothes_top_accuracy_test = evaluate_model(self.clothes_top_model, self.clothes_top_data_test)
        self.pants_accuracy_test = evaluate_model(self.pants_model, self.pants_data_test)
        self.shoes_accuracy_test = evaluate_model(self.shoes_model, self.shoes_data_test)
        self.outers_accuracy_test = evaluate_model(self.outers_model, self.outers_data_test)"""
        self.clothes_top_accuracy_test, self.clothes_top_precision_test, self.clothes_top_recall_test = evaluate_model(self.clothes_top_model, self.clothes_top_data_test)
        self.pants_accuracy_test, self.pants_precision_test, self.pants_recall_test = evaluate_model(self.pants_model, self.pants_data_test)
        self.shoes_accuracy_test, self.shoes_precision_test, self.shoes_recall_test = evaluate_model(self.shoes_model, self.shoes_data_test)
        self.outers_accuracy_test, self.outers_precision_test, self.outers_recall_test = evaluate_model(self.outers_model, self.outers_data_test)

    
    def predict_and_evaluate(self):
        def predict_recommendation(test_data, model):
            predictions = model.transform(test_data)
            predictions = predictions.withColumn("predicted_recommend", F.col("prediction"))
            return predictions
    
        # 각 카테고리별 예측 데이터 생성
        clothes_top_predictions = predict_recommendation(self.clothes_top_data_test, self.clothes_top_model)
        pants_predictions = predict_recommendation(self.pants_data_test, self.pants_model)
        shoes_predictions = predict_recommendation(self.shoes_data_test, self.shoes_model)
        outers_predictions = predict_recommendation(self.outers_data_test, self.outers_model)
    
        self.clothes_top_predictions_result = clothes_top_predictions.select("productId", "category", "recommend", "predicted_recommend")
        self.pants_predictions_result = pants_predictions.select("productId", "category", "recommend", "predicted_recommend")
        self.shoes_predictions_result = shoes_predictions.select("productId", "category", "recommend", "predicted_recommend")
        self.outers_predictions_result = outers_predictions.select("productId", "category", "recommend", "predicted_recommend")
    
        # 기존 정확도 출력
        print(f"Clothes Top Train Accuracy: {self.clothes_top_accuracy_train:.4f}")
        print(f"Pants Train Accuracy: {self.pants_accuracy_train:.4f}")
        print(f"Shoes Train Accuracy: {self.shoes_accuracy_train:.4f}")
        print(f"Outers Train Accuracy: {self.outers_accuracy_train:.4f}")
    
        print(f"Clothes Top Test Accuracy: {self.clothes_top_accuracy_test:.4f}")
        print(f"Pants Test Accuracy: {self.pants_accuracy_test:.4f}")
        print(f"Shoes Test Accuracy: {self.shoes_accuracy_test:.4f}")
        print(f"Outers Test Accuracy: {self.outers_accuracy_test:.4f}")
    
        # 전체 예측 결과 통합
        all_predictions_result = self.clothes_top_predictions_result \
            .union(self.pants_predictions_result) \
            .union(self.shoes_predictions_result) \
            .union(self.outers_predictions_result)
    
        # 카테고리별 TP, FN, FP, TN 계산 및 출력
        categories = ["clothes_top", "pants", "shoes", "outers"]
        for category in categories:
            category_predictions = all_predictions_result.filter(F.col("category") == category)
    
            tp = category_predictions.filter((F.col("predicted_recommend") == 1) & (F.col("recommend") == 1)).count()  # 1 -> 1
            fn = category_predictions.filter((F.col("predicted_recommend") == 0) & (F.col("recommend") == 1)).count()  # 1 -> 0
            fp = category_predictions.filter((F.col("predicted_recommend") == 1) & (F.col("recommend") == 0)).count()  # 0 -> 1
            tn = category_predictions.filter((F.col("predicted_recommend") == 0) & (F.col("recommend") == 0)).count()  # 0 -> 0
    
            recall = round(tp / (tp + fn), 2) if (tp + fn) > 0 else 0.0
            precision = round(tp / (tp + fp), 2) if (tp + fp) > 0 else 0.0
    
            print(f"""
            Category: {category}
            True Positive (TP): {tp}
            False Negative (FN): {fn}
            False Positive (FP): {fp}
            True Negative (TN): {tn}
            Recall: {recall}
            Precision: {precision}
            """)
    
        # 전체 데이터 TP, FN, FP, TN 계산 및 출력
        tp = all_predictions_result.filter((F.col("predicted_recommend") == 1) & (F.col("recommend") == 1)).count()
        fn = all_predictions_result.filter((F.col("predicted_recommend") == 0) & (F.col("recommend") == 1)).count()
        fp = all_predictions_result.filter((F.col("predicted_recommend") == 1) & (F.col("recommend") == 0)).count()
        tn = all_predictions_result.filter((F.col("predicted_recommend") == 0) & (F.col("recommend") == 0)).count()
    
        recall = round(tp / (tp + fn), 2) if (tp + fn) > 0 else 0.0
        precision = round(tp / (tp + fp), 2) if (tp + fp) > 0 else 0.0
    
        print(f"""
        Overall Results:
        True Positive (TP): {tp}
        False Negative (FN): {fn}
        False Positive (FP): {fp}
        True Negative (TN): {tn}
        Recall: {recall}
        Precision: {precision}
        """)



def save_results(self):
        final_predictions_result = self.clothes_top_predictions_result.union(self.pants_predictions_result) \
            .union(self.shoes_predictions_result).union(self.outers_predictions_result)

        final_predictions_result = final_predictions_result.toPandas()

        final_predictions_result_file = os.path.join(self.output_dir, "predict_output.csv")
        final_predictions_result.to_csv(final_predictions_result_file, index=False, encoding="utf-8-sig")
        print(final_predictions_result.shape)
        print("Success data save")

def main():
    # product_rankings_path, product_keywords_path
    product_rankings_path = "hdfs://sandbox-hdp.hortonworks.com:8020/user/maria_dev/term_project_data/processed/product_rankings.csv"
    product_keywords_path = "hdfs://sandbox-hdp.hortonworks.com:8020/user/maria_dev/term_project_data/processed/product_keywords.csv"
    output_dir = "./data/output/"

    recommendation = ProductRecommendation(product_rankings_path, product_keywords_path, output_dir) 
    
    recommendation.preprocess_data()
    recommendation.vectorize_keywords()
    recommendation.train_models()
    recommendation.evaluate_models()
    recommendation.predict_and_evaluate()
    recommendation.save_results()

if __name__ == "__main__":
    main()
